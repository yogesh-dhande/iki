# @ikisci/component

Types and helpers for writing a plugin's component: a piece of UI a plugin
adds to Iki's pages, such as a plate map, a box map or a bookings calendar.
The manifest a component declares is described in the
[plugin spec](../../../spec/README.md#components); the decisions behind it
are [ADR 0084](../../../docs/adr/0084-plugin-components-run-in-sandboxed-frames.md)
and [ADR 0117](../../../docs/adr/0117-components-show-many-entities-in-more-places.md).

A component is a custom element, defined in one JavaScript module with
everything it needs bundled in. Iki runs it in a sandboxed frame of its own:
it has no network, no cookies or storage, and no way to call Iki's API.
Everything it shows, Iki gives it; everything it does, it asks Iki to do.

## What it's given

Iki sets three properties on the element, each again whenever it changes:

- `place`: where it is, `{ slot, label }`. `slot` is `entity.tab`,
  `search.results`, `project.page`, `person.home` or `app.panel`.
- `entity`: on an entity's tab, or in an app's panel that names one, that
  entity, as the person viewing it may read it.
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
import { more, open, refine, select, type Entities, type Place } from "@ikisci/component";

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
}
customElements.define("acme-calendar", Calendar);
```

Each helper sends an event from the element: `iki-select`, `iki-more`,
`iki-refine` or `iki-open`. Iki checks every one before acting on it, and
again on its server:

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

## Building one

Bundle the module with everything it imports, such as with esbuild or
Vite's library mode, into the one file the manifest's `module` names. This
package's helpers are a few lines each, and its types cost nothing at run
time.
