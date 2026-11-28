# 0170. A component can offer its plugin's actions on its entity

- Status: Accepted
- Date: 2026-11-20
- Refines: [0084](0084-plugin-components-run-in-sandboxed-frames.md),
  [0117](0117-components-show-many-entities-in-more-places.md)
- Extended by: [0286](0286-the-component-package-is-the-frame-protocols-one-source.md),
  the frame protocol's one source, versioned and published

## Context

A plugin's tab often shows what one of its actions computed: the complex
plugin's Composition shows the formula and masses `compute_composition`
worked out, and the plasmid plugin's Map draws the parts `annotate_plasmid`
found. Each plugin also has a trigger that runs that action on new records.
Triggers belong to a project and stay off until someone turns them on (ADR
0080), so a new record's tab is often empty.

The empty tabs said the value is computed "when it's registered", or that the
plasmid is annotated "when a project gets it". With the trigger off, neither
was true. Someone who wanted the value had to leave the tab, find Run action,
pick the action from the list and pick the record again. Benchling and
SnapGene put the action that fills an empty panel inside the panel.

A component can't start a run itself. Its code runs in a frame with no access
to the person's session. ADR 0084 says writes, runs and navigation go through
Iki's own screens, with their confirmations, and the component can only tell
the page what a person did.

## Decision

**A component declares the actions it may offer, under `events.run`:**

```yaml
events: { run: { actions: [compute_composition] } }
```

Each one is its own plugin's action, so it's reviewed with the component, as a
trigger's action is. Each must have an input that takes the type of every tab
the component is on. The validator refuses an action the plugin doesn't have
(`component-unknown-action`), one with no input that takes the tab's entity
(`component-action-takes-no-entity`), and offers from a component that has no
entity tab (`component-run-needs-a-tab`). `GET /components` lists the offers,
qualified, as `runs`.

**The page tells the element what the viewer may run.** On an entity's tab,
`place.runs` lists the offered actions that the person viewing the tab may run
on that entity there and then. The conditions are:

- the action is available on this deployment;
- its tier is one the person may run in the entity's project;
- one of its inputs takes the entity;
- if the action changes the entity, the person may change it;
- the entity isn't archived.

Until the page knows all of this, the list is empty. The element shows an
action's button only when the action is in the list, so nobody is offered a
button that would only end in a refusal.

**Asking opens Iki's run page, nothing more.** The element dispatches
`iki-run` with `detail.action`. The bridge passes it to the page as a `run`
message. The page drops any action the component didn't declare, and any its
place didn't offer. For the rest it opens `/projects/<slug>/runs/new/` with the
action and the entity chosen. The person checks the run there and starts it,
and the server checks everything again, as it does for any run.

A component should describe what it can't show truthfully. It names the action
that would fill it and the trigger a project can turn on. It doesn't claim the
trigger will run, because it can't know that.

## Consequences

- An empty tab carries its own way out: Compute composition, or Run
  annotation, for whoever may run them, and the plain facts for everyone else.
- A component still can't run anything, change anything or reach the network.
  The most a compromised one can do is open the run page with one of its own
  plugin's actions chosen, and the person still has to start it.
- The element is told a little about the viewer: which of its declared actions
  they may run. That is less than its tab already shows them.
- Tabs only. A place that shows many entities has no single entity to run on,
  and a selection already offers Run action (ADR 0117).
- A frame's address is its module's digest, and browsers keep a frame for
  ever, along with the bridge inlined in it. A module that dispatches
  `iki-run` is a new module, so its frame is new too and carries this
  bridge. A later change to the bridge that existing modules rely on would
  need the bridge's digest in the frame's address as well.
