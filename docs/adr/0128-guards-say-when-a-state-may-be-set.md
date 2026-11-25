# 0128. Guards say when a state may be set: allowed moves, four eyes and more

- Status: Accepted
- Date: 2026-11-18
- Refines: [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
  [0127](0127-plugins-rules-are-checked-at-every-save.md),
  [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md)

Updated by [ADR 0211](0211-lots-inventory-samples-and-requests.md). The sample analyst signs submission, and an independent reviewer signs the measured results. Lot disposition and QA release live with the lot.

## Context

ADR 0125 left workflow policy to plugins. A state says which lab roles may
set its field to it, from any other state. Which state may follow which,
who mustn't approve their own work, and what else must be true first were
deferred to guards. GxP labs expect all three. A sample is approved only
from review. Whoever submitted it doesn't approve it (four eyes, 21 CFR
211.194's second-person review). A lot isn't released while it's
quarantined. Jira's workflow conditions and validators and Salesforce's
validation rules on a picklist's prior value are the precedent: conditions
over the change, the record and who's making it.

## Decision

**A state declares `guards`**, by name, each a CEL `check` that must be true
for anyone to set the field to that state, with a `message` saying why not, an
optional `when`, and a `description`. `set_by` still says who may. Guards say
when. Every guard of the state must hold. A guard reads:

- `actor`: the person setting it (their ID, empty for a service account), the
  agent acting for them (`{name, client_id}`, null without one, ADR 0072),
  their lab roles in the record's project (`roles`, by qualified name and by
  the plugin's own short names), and `service_account`;
- `record`: the record's values, every state field among them, with each
  reference one step deep, as a rule reads it (ADR 0127);
- `state`: the change (`field`, `from`, `to`);
- `history`: each state field's changes, oldest first, each
  `{from, to, by, agent, at, reason}`. A record's initial states are a change
  at its creation;
- `last`: for each state field, the latest change into each of its states,
  null if it's never been in it;
- `edits`: for each state field and state, the editors and changed fields
  since its most recent entry. [ADR 0179](0179-a-corrected-result-goes-back-for-review.md)
- `holds`: the rules holding it for review;
- `signatures`: for a state set by signing for it, the signatures given for
  the change so far, each `{index, meaning, by, at}`, empty for any other;
- `now` and `today`.

So a plugin writes allowed moves (`state.from == "in_review"`), four eyes
(`last.status.in_review == null || last.status.in_review.by != actor.person`),
a person rather than an agent (`actor.agent == null`), and rules across state
fields (`record.custody != "quarantined"`). Guards are type-checked when their
plugin is validated, against the type's fields and states. A state that's not
one of its field's is refused there, as an enum value is. Limit: 20 guards per
state.

**A guard fails closed.** A guard that's false refuses the change with its
message. So does one that can't be worked out, for any reason, including an
empty value (which a save rule would take as not applying): a guard that can't
say yes says no. One whose `when` is false, or empty, doesn't apply. A refused
change is 409 `guard_refused`, naming the `guard`. It's checked under the
record's lock, after `set_by`, with the record as it is.

**Holds come first.** While a rule holds a record for review (ADR 0127), it
can't be set to a state that locks it, whatever its guards say: 409 `refused`,
naming the holds.

**A signed state's guards are checked at every signature.** A state that
needs electronic signatures (ADR 0126) isn't set directly: its last signature
sets it. Its guards and holds are checked as each signature is started and
again as it's given, with that signer acting. So nobody signs a change a guard
would refuse, and the last signature, which makes the change, checks them once
more under the record's lock. A guard that refuses one signer, such as four
eyes, says so where they'd sign. A guard that no longer holds by the last
signature (a hold opened, say) stops it, and the signatures already given stay
on the revision, waiting.

Checking only at the first signature would let the record's state change
between signatures, such as another state field set, unchecked. Checking only
at the last would let people sign a change that can't be made. And it's what
lets a guard say whether one person may give two of a change's signatures,
which ADR 0126 leaves to the plugin: `signatures.all(s, s.by != actor.person)`
refuses the second, as it starts and, if two were started at once, as it's
given.

**State controls explain.** `can.states` lists, for each state the reader
could set, whether they may and why not: their role, the record's condition,
its holds or a guard, with the guard's name (`guard`). The web app shows the
reason by each state it won't set, and the MCP tool and SDKs carry it.

## Consequences

- example_lims's Sample keeps four eyes again: whoever submitted it for review
  can't approve it, and QA can't release what they approved. It moves only
  along its process. Its approve, release and reject states are signed, which
  is a person's act already, so it has no guard keeping agents out of them.
- A guard decides from what Iki keeps (who, through what, when, why, every
  state's history), so plugins don't need state of their own for policy.
- Guards read the record as it is, at each signature too.
- A guard can't count other records ("two of three reviewers"). Such policies
  wait for graph queries (step 59).

## Alternatives considered

- **A transition graph in core** (ADR 0118). One graph for every lab, and every
  exception would be a core feature.
- **`not_by` in core** (ADR 0119). It covers one separation-of-duties policy,
  and guards cover it and others.
- **Guards fail open on empty values, as save rules do.** A missing history
  would then let anyone approve. Policy that can't be decided refuses.
