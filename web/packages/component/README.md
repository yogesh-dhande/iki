# @ikisci/component

Iki's component frame protocol, for writing a plugin's component: a piece of
UI a plugin adds to Iki's pages, such as a plate map, a box map or a bookings
calendar. It holds what a component is given and the helpers that ask Iki
for things, the validators Iki's own page reads a frame's messages with
(`@ikisci/component/host`), and `iki-component-bundle`, which builds a
component's module. It's published with each Iki release, at Iki's version,
and is the protocol's one source: Iki's page imports it, and its bridge is
tested against it.

The manifest a component declares is described in the
[plugin spec](../../../spec/README.md#components); the decisions behind it
are [ADR 0084](../../../docs/adr/0084-plugin-components-run-in-sandboxed-frames.md),
[ADR 0117](../../../docs/adr/0117-components-show-many-entities-in-more-places.md),
[ADR 0170](../../../docs/adr/0170-a-component-can-offer-its-plugins-actions-on-its-entity.md)
and [ADR 0286](../../../docs/adr/0286-the-component-package-is-the-frame-protocols-one-source.md).

A component is a custom element, defined in one JavaScript module with
everything it needs bundled in. Iki runs it in a sandboxed frame of its own:
it has no network, no cookies or storage, and no way to call Iki's API.
Everything it shows, Iki gives it; everything it does, it asks Iki to do.

## What it's given

Iki sets three properties on the element, each again whenever it changes:

- `place`: where it is, `{ slot, label, runs }`. `slot` is `entity.tab`,
  `search.results`, `project.page`, `person.home` or `app.panel`. On an
  entity's tab of a component that declares `events.run`, `runs` lists the
  actions it offers that the person viewing it may run on the entity now.
- `entity`: on an entity's tab, or in an app's panel that names one, that
  entity, as the person viewing it may read it: its `values`, the entities
  they name in `refs` and the people they name in `people`, by ID.
- `entities`: for a component that declares `entities` in its manifest,
  what fills its place, a page at a time: `items` (each with `id`, `hrid`,
  `title`, `type`, `revision`, `project`, the fields it reads in `values`
  and the entities they name in `refs`). For a person named by those
  fields, `item.people?.[id]?.name` gives their display name. The page also
  says whether there are `more`, whether
  it's `limited` (it holds as many as its `limit`), whether a page is
  `loading`, an `error` if its search was refused, and the `refinement` in
  force.

Iki computes `entities` on its server, as the person viewing the page may
read them, from the place's search: the tab's, the grid's, the project
page's or the person's home's. A component never names that search, and
sees only the fields it declared.

## What it may ask for

```ts
import { more, open, refine, run, select, type Entities, type Place } from "@ikisci/component";

class Calendar extends HTMLElement {
  set entities(entities: Entities) {
    /* draw entities.items */
  }
  set place(place: Place) {
    /* show place.label */
  }
  pick(hrid: string) {
    select(this, [hrid]); // offers Iki's run page; the type is the manifest's `events.select`
  }
  next() {
    more(this); // the next page, while entities.more says there is one
  }
  month(start: string, end: string) {
    // Narrows what's shown, on fields it reads; null clears it.
    refine(this, {
      where: {
        all: [
          { field: "starts_at", op: "gte", value: start },
          { field: "starts_at", op: "lt", value: end },
        ],
      },
    });
  }
  show(hrid: string) {
    open(this, hrid); // Iki opens the entity's page: only one it was given
  }
  book() {
    // Opens Iki's run page for an action the manifest's `events.run` offers, with the
    // tab's entity chosen: offer it only when place.runs has it.
    run(this, "acme/equipment.book_instrument");
  }
}
customElements.define("acme-calendar", Calendar);
```

Each helper sends an event from the element: `iki-select`, `iki-more`,
`iki-refine`, `iki-open` or `iki-run` (`EVENTS` names them). Iki checks every
one before acting on it, and again on its server:

- A refinement is added to the place's search, so it can only narrow it. It
  names only the fields the component reads, and `hrid`, with at most 20
  conditions, 4 levels deep. A sort replaces the place's; text is added only
  when the place has none.
- `open(element, hrid)` opens the page of an entity the component received.
  `open(element, recordUuid, revision)` opens the historical revision as JSON. The
  host requires a readable reference and the exact `record_id` and `record_revision` in
  a stored row. A row for a registry-backed record uses `entry_id` for the reference and can open the corresponding `entry_revision`.
- Picking entities offers Iki's run page, with those of the declared type
  the person can read. A component writes nothing itself.
- `run(element, action)` opens Iki's run page with the action and the tab's
  entity chosen, only for an action in `place.runs`. The person checks the
  run there and starts it.

## Building one

A frame loads exactly one module, so a component is bundled with everything
it imports. Write each component's source in the plugin's `web/src/`, as
`web/src/<name>.js` (or `.ts`), with any helpers in subdirectories, and keep
`web/` a package of its own: Iki's builds leave `web/` out of what they ship,
and serve `components/<name>.js`, the file the manifest's `module` names.

```json
{
  "private": true,
  "type": "module",
  "devDependencies": { "@ikisci/component": "0.1.0", "rolldown": "1.3.3" },
  "scripts": {
    "bundle": "iki-component-bundle ..",
    "check": "iki-component-bundle --check .."
  }
}
```

`npm run bundle` in `web/` writes `components/<name>.js` for each source;
`npm run check` writes nothing and fails if one isn't what its source
builds, as the plugin template's CI runs it. Use the `@ikisci/component` of
the Iki release your deployment runs, so the protocol it speaks is that
Iki's. The bundler refuses a module that would import anything at run
time, because the frame's policy loads nothing else.

## The frame protocol

Iki's page and its bridge in the frame exchange messages through one
`MessagePort`, as `Connect`, `HostMessage` and `FrameMessage` describe.
`PROTOCOL` is their version: the page names it when it connects and the
bridge when it's ready, and each refuses one it doesn't speak. A frame's
address carries the digests of both the module and the bridge, so a browser
never runs a frame it kept with another Iki's bridge.
`@ikisci/component/host` has the page's validators, `readMessage` and
`readRefinement`, for a component's own tests.
