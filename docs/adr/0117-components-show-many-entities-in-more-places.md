# 0117. Components show many entities, in more places than a tab

- Status: Accepted
- Date: 2026-11-17
- Refines: [0084](0084-plugin-components-run-in-sandboxed-frames.md),
  [0090](0090-apps-are-compositions-of-ikis-components.md)

## Context

A plugin's component was a tab on an entity's page, and got that one entity
(ADR 0084). Apps were made only of Iki's own components (ADR 0090). Much of
what labs look at is many entities at once, laid out as only a plugin knows
how:

- a freezer box or a plate, each tube or well where it is;
- an instrument's bookings, as a calendar;
- a family's animals, as a pedigree;
- everything assigned to a person, across their projects.

These belong in more places than a tab: on a project's page, on a person's
home, as another way to see the grid's results, and in apps beside Iki's own
grids and viewers. Benchling's box and plate views, LabArchives' and
iLab's instrument calendars, and Jira's "assigned to me" are what people
expect to find there.

Each place needs a rule for what the component gets. A component's code is
the plugin's, and could be a compromised dependency, so it must not be able
to ask for more than its place shows, nor learn anything its viewer can't
read.

## Decision

**A component declares the many entities it shows.** Beside `entity`, the
one entity whose page it's on, a component may declare `entities`: the type
it shows (with its subtypes), the fields it reads of each, and the most it
holds at once (`limit`, 1 to 1,000, 500 if not given). It gets only those
fields, beside each entity's ID, HRID, type, title, revision and project, and
the titles of the entities those fields refer to that its viewer can read.
The fields are in its manifest, so a review sees what a plugin's code can
read, as it sees what an action can reach.

**Each place says which entities fill it.** A component's slots name its
places, and each has a contract:

| Slot             | Where                                   | What it gets                                                                         |
| ---------------- | --------------------------------------- | ------------------------------------------------------------------------------------ |
| `entity.tab`     | a tab on the pages of a type's entities | `entity`; and `entities`, if the slot declares a `search`, which can name the entity |
| `search.results` | the grid, shown another way             | `entities`: the grid's search, as the person set it                                  |
| `project.page`   | a section on a project's page           | `entities`: the slot's `search`, in that project                                     |
| `person.home`    | a section on a person's home            | `entities`: the slot's `search`, in every project they read, which must name them    |
| an app's panel   | beside Iki's own components (below)     | `entity` and its tab's `search`, or `entities` from the panel's own search           |

A declared `search` is a search as the API takes it, of the component's
type: conditions (`where`), a `sort` and its direction. A condition's value
can be the place's context and nothing else: `{{ entity }}`, the entity whose
page it is, for an entity tab (a box's tubes are those whose `box` is it), and
`{{ me }}`, the person viewing it, compared today with `created_by`. Step 57
adds fields that name a person, and they'll take `{{ me }}` the same way, so
"assigned to me" is `{field: assignee, op: eq, value: "{{ me }}"}` with no new
syntax. A section on a person's home must name `{{ me }}`: it's theirs, not a
list of everything they can read. A declared search has at most 20
conditions, nested at most 4 deep, as a refinement does, so the two joined
stay within what the API's searches take. The validator checks each
search's fields, its size and its context's places; the server compiles
each one again when it serves it.

**The server computes every set, as the grid's search.**
`POST /components/{name}/entities` answers with a page of what fills a
place: an entity's tab (`entity`), a search's results (`search`), a
project's page (`project`) or a person's home (`home`). It runs the search
with the code `POST /entities/search` runs, as the caller, with their
roles in each project, their token's scopes and projects, and the records
and registry entries they can read (ADR 0115). A place they can't read
(another project, an entity they can't open) answers 404. A component
that doesn't declare a place, or declares no `entities`, is refused there.
For each item, `people` maps the IDs in its declared person fields to
display names visible on that record. A verification tab can show who
reviewed the evidence while supplying only the name to the component.

**A component may page and refine, never widen.** The frame can ask for the
next page (`iki-more`) and narrow what it was given (`iki-refine`: conditions,
a sort, text in HRIDs and titles), as a calendar asks for one month at a
time. The page passes these on; the frame never names the search itself.

- A refinement's conditions are added to the place's with `all`, so they can
  only narrow it. A sort replaces the place's. Text is added only when the
  place's search has none.
- A refinement names only the fields the component reads, and `hrid`, so it
  can't learn about others by narrowing on them. It has at most 20
  conditions, nested at most 4 deep.
- The page checks each message before acting on it, and the server checks
  the refinement again: an endpoint anyone can call takes nothing on trust.
- A page holds at most `limit` entities for a frame, at most 100 a request,
  and one request at a time; a new refinement starts again from the first
  page.

**The sandbox, its policy and its messages stay as they were.** A component
still runs in `<iframe sandbox="allow-scripts">` with no network, no
session and no origin of its own; its data comes only through Iki's bridge,
on the port the page handed it. The bridge sets `entity`, `entities` (the
items, whether there are more, whether it's at its limit, and the
refinement in force) and `place` (which slot, and its label) on the
element. The page accepts `ready`, `resize`, `select` (of the declared
type), `more`, `refine` and `open` (an entity it was given, whose page Iki
opens), and nothing else.

**Components don't write.** What a person picks in one is read back from the
API, as they may read it, and offered to Iki's run page, which launches with
its own permission checks, approvals and audit. Opening an entity is Iki's
navigation to Iki's page. Anything a component might one day change goes
through the API as the person, with the same checks and audit as the UI,
behind a declaration the checks can see, never through a channel of its own.

**Apps place plugins' components beside Iki's own.** A panel names one by
its full name, as ADR 0113 names everything of another plugin's:
`"component": "acme/lab.box_map"`, beside `iki.entities`. Its props are
either `entity` (an HRID, or one expression, as a viewer's are), for a
component with a tab on that entity's type, or a search as `iki.entities`
takes it (`type`, `scope`, `where`, `q`, `sort`, `descending`), of the
component's type, for one that declares `entities`. It offers later panels
the `selection` a person made in it, read back from the API. Saving checks
that the component is installed and fits the props, as it checks Iki's own,
and the guide (`GET /apps/guide`, `describe_apps`) lists the installed ones.

**A missing or broken component costs only its own panel.** A panel whose
component is no longer installed (its plugin removed or detached), or no
longer fits, says so in its card, offers an empty selection, and the panels
after it go on. A component that doesn't start within 15 seconds, or opens
another page, is taken away with a message naming its plugin, as on a tab.
A section on a project's page or a person's home shows only once its search
finds something, so a component with nothing to show takes no room.

## Consequences

- Box and plate maps, calendars, pedigrees and worklists are plugins'
  components, shown where people look, with no more access than the grid's.
- What a component can read is written in its manifest: its type, its
  fields, its places' searches. Reviews and the validator see it.
- `{{ me }}` is the hook for step 57: a person field takes it as
  `created_by` does now.
- A component can still leak what it was given through the channels ADR
  0084 and 0102 describe (a frame opening another page in itself), and now
  it may be given more. Its fields and limit bound that; review, pinning and
  signing remain the guard, as for all plugin code.
- Each place costs one frame and one search, so sections are few: a
  project's page or a person's home shows at most eight.

## Alternatives considered

- **Let the frame send its own searches.** Rejected: it could ask for any
  type and any field its viewer reads, and the page would have to guess what
  the place meant.
- **The page builds every search, and calls `POST /entities/search`.**
  Rejected: declared searches with `{{ me }}` and `{{ entity }}`, the fields
  a component reads, and the rules on refinements would be checked only in
  the browser.
- **Send every field.** Rejected: a calendar has no need of a booking's
  notes, and what a plugin's code could pass on is better small and
  written down.
- **Project Admins choose which components their page shows.** Not now: a
  section shows only when its search finds something, and an Admin who
  wants a different page chooses an app as the project's dashboard, which
  can place any component.
