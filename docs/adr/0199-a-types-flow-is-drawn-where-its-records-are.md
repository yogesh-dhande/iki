# 0199. A type's flow is drawn where its records are

- Status: Accepted
- Date: 2026-11-22

## Context

A plugin defines a state field's states, who sets each, whether it locks the record,
the signatures it needs, its guards and where a correction returns it
([ADR 0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
[0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
[0128](0128-guards-say-when-a-state-may-be-set.md),
[0179](0179-a-corrected-result-goes-back-for-review.md)). Until now, the only complete
description was the plugin's YAML. A QA lead choosing between the Research and Regulated QC
review flows ([ADR 0198](0198-a-project-applies-a-versioned-assay-starter.md)) should be able
to see each one without reading that YAML. So should an analyst checking what comes next for
a record.

## Decision

Each type's page has a section called "How records move". It contains a diagram for each
state field and the same flow written out in words. The state panel on a record's page shows
the same diagram behind a "How it moves" toggle. There, the record's current state is marked
"Now", and the states the reader may set next are marked "You can".

**The flow comes from one place.** `GET /api/v1/entity-types/{type}/flow` (ADR 0198) answers
with each state field's states and the transitions its guards allow. Each transition says
whether it is conditional: the server reads the guards knowing only the field and the two
states, so a guard that needs the record, the person or the history keeps the transition and
marks it conditional. The web app reads the flow only through `typeFlowQuery` in
`web/app/src/flow.ts`, and never works it out itself.

**The state shows who sets it and what they sign.** In Iki, `set_by` and `signatures` belong
to the destination state, so every arrow into a state would carry the same label. The diagram
shows that information once, on the state's node, where the arrows arrive. The node shows the
lab roles, a pen mark with each signature's name and meaning (such as "QA release
(Approval)"), a dot on the initial state and a lock on each locking state. A state that can be
reached from every other state gets a single "from any state" stub, not one arrow from each
state. Corrections (`on_edit`) are dashed arrows.

**The diagram is laid out without a library.** States follow their declared order: left to
right when they fit, otherwise top to bottom. A move to the next state is a straight arrow.
Other moves are arcs. Forward arcs run above the row, or to the right of the column. Backward
arcs run below the row, or to the left of the column. Arcs that would overlap get separate
lanes. Nodes are HTML buttons, so their text wraps and takes the theme's tokens. The lines are
SVG and are hidden from assistive technology. In a wide column layout, the words sit beside
the diagram.

**It's accessible as text.** Each state is a button. Its name starts with the visible label,
and its description is that state's entry in the written flow: who sets it, from where, what
they sign and attest, what it locks, where an edit returns it, and its guards' messages. Tab
and the arrow keys move between states. Choosing a state highlights its arrows, and Escape
clears the choice. On a record's page, the written flow is visually hidden but still describes
each button, including why the reader can't set a state.

**It moves into `@ikisci/ui` later.** The diagram takes plain data: a flow and lab-role labels.
It reports the chosen state through one callback, so it can become a custom element with an
`iki-flow-select` event (ADR 0191).
The layout and wording in `flow-model.ts` don't use React. The component stays in the app
until the package foundations are merged.

## Consequences

- A flow can't contradict its plugin. The web app's tests draw the real example_lims, lots
  and plate assays types from `flow-fixtures.json`, and a server test checks that file
  against what the flow endpoint serves for those plugins.
- A starter's setup screen (ADR 0198) draws each review flow it offers with the same
  diagram, from its result type's flow, so an Admin choosing between Research and
  Regulated QC sees what each asks of people.
- The diagram doesn't mark conditional transitions differently. Almost every move into a
  signed or locking state depends on the record or on who is acting, so a mark would add
  noise. The guards' messages explain those conditions in words.
- A field with many states that can be reached from anywhere, such as an analysis request's
  status, appears as states with stubs and few arrows. That is accurate: the plugin imposes
  no order.
