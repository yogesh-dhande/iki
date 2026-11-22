# 0176. The app is laid out around a sidebar, and what you can read

- Status: Accepted
- Date: 2026-11-21
- Refines: [0037](0037-entity-search.md)

## Context

Iki's frame was a header with one row of links: My work, Projects, Types,
Teams, People, Settings, Costs, Compute and Audit. Everything a scientist
works with, from records and requests to runs, sat inside a project, behind
up to nine tabs. Browser QC of the dev cluster showed what this costs:

- **Records were found one project at a time.** A scientist in four projects
  looked for a plasmid four times. Yet a record can belong to several
  projects, and the search API already returned every record its reader can
  read when no project was given.
- **The header mixed work with administration.** Compute, Costs and Audit
  sat beside My work. Most people never use them, and there was no room left
  for more.
- **Every field said who last changed it.** A line under each value named a
  person and a time. On a record with twenty fields, that is twenty lines of
  audit trail mixed into the data, which made the values hard to read.
- **Pages were loose.** They used tall rows, wide gaps and large type, so a
  table showed a dozen rows where it had room for thirty.

Benchling, GitHub, Linear and Slack all lay out their work around a left
sidebar. Its top half is what you work with across everything you belong to;
then come your own places (saved views, projects); administration has an
area of its own.

## Decision

**A sidebar replaces the header's navigation,** on every page, both the
app's and the server-rendered ones. Its sections, from top to bottom:

- **Search everything (⌘K)**, then My work and the inbox (notifications,
  with their unread count).
- **Entities**, which expands to All entities and to each type that has
  records you can read, with counts.
- **Requests** and **Runs**.
- **Saved views**: your saved searches.
- **Projects**: the projects you belong to, a button to make one if you may,
  and All projects.
- **Organization**: Types, People and teams, and Admin for those with any
  administrative permission.
- **You**: your account, settings and sign-out.

On a narrow screen, the sidebar opens as a drawer from a menu button.

**Entities, Requests and Runs span every project you can read.** They are
not repeated under each project. Each list has a Project column and a
Project filter. A record in several projects shows each one. The lists find
only what the reader may read, by the same rules as reading one record. A
record in a project you are not in never appears, not even in a count. The
same holds for each type's table at `/entities/types/<type>`, and for the
counts in the sidebar.

**Search everything searches across kinds.** It covers records, requests,
runs, projects, people and saved views. Results are grouped by kind; each
shows its project, and each kind's group links to its full list. It uses
each kind's own reading rules, so it finds nothing its reader couldn't open.
`GET /api/v1/search` answers it:

- **Records and requests** are found as the entity search finds them, in
  every project their reader can read. Archived ones aren't searched, as the
  lists hide them until asked. **Runs** are those of projects whose data
  their reader can read; **projects**, those they can see; **saved views**,
  their own and those shared with a project they read.
- **People** are everyone, for those who manage the organization's people.
  Anyone else finds the people that the members pages of their projects and
  teams show them, and a person opens on the members page they share.
- **Text needs three characters** to search records, requests and runs,
  because their trigram indexes need three. The start of an HRID (`PRT-0`)
  is found by the HRIDs' own index, and a whole HRID comes first.
- **Each group counts what it found up to 1,000**, and says "1,000+" past
  that. A group whose search runs out of time says so; it gives no count
  rather than an estimate.
- **With nothing typed, it shows what its reader changed or ran lately.**
  That comes from the server, so no browser keeps a list of what someone
  opened.

The palette is a combobox (WAI-ARIA's pattern). Focus stays in its text box,
and the arrow keys move through the results. Each group's link to its full
list is its last option, so the arrows reach it. ⌘K (Ctrl+K) opens it, as
does anything that dispatches an `iki:search` event on the window. The
server-rendered pages load the palette on its own, as a second entry of the
web app's build that shares the app's chunks, so ⌘K opens the same palette
there.

**A project's page holds what belongs to the project alone.** Its tabs are
Overview, Entities, Requests, Runs, Automation, Members and Settings.
Workflows, triggers and webhooks go under Automation, so they no longer take
three tabs. Its Entities, Requests and Runs tabs are the top-level lists,
filtered to the project.

- **Its heading** says where it is (Projects / its name), its first line of
  description, how many members it has and the reader's role. **Watch** chooses
  how the reader is told of what happens in it, as GitHub's does; **New** makes a
  record, a run or a workflow run there, for those who may.
- **Automation's three are under it.** Workflows, triggers and webhooks are at
  `/projects/<slug>/automation/workflows/`, `triggers/` and `webhooks/`, with
  their new and detail pages, as a row of their own under the Automation tab.
  `/projects/<slug>/automation/` opens the first of them the reader may see:
  workflows, or webhooks for an Admin who can't read the project. Their old
  addresses are gone, with no redirect: Iki wasn't deployed anywhere yet.
- **The Overview** counts its records by type, each opening that type's table
  held to the project; lists its latest changes (`GET /projects/{id}/changes`:
  its records' revisions, read by the same rules as its Entities tab); and
  beside them says what it is, its latest requests, its members and its
  automation.
- **Each project has a color,** a small square beside its name. Projects have
  no color to choose, so each takes one of eight from its ID: the same
  everywhere and for everyone, at 3:1 or more against the page in both themes.
  People's initials take theirs the same way.

**A record shows its values. Who changed them is on demand.** The line under
each field goes. Each field has a small history button that shows its last
change and links to the trail. The record has an **Audit trail** tab: every
revision with its before and after, its signatures and its reasons, filtered
by person and field, with a By field view and an export for inspection. A
record in several projects has a project picker for its project values.

- **A list of objects never reads as JSON.** A plasmid's annotated features
  are a small table of the keys its objects hold plain values in (name, type,
  start to end as one position, strand); keys holding objects, such as a
  feature's qualifiers, are in the audit trail and the record's export. A
  grid's cell, which has one line, says how many and names them: "3 · tet
  promoter, TcR, ori". One reading serves both, so a value reads the same on
  the page, in the grid and in the trail's By field view.
- **Its rail draws its features as a map,** from its own sequence viewer, or,
  for a record whose sequence is another's by reference (a plasmid's is its DNA
  sequence's, ADR 0163), from that record's bases and its own features field,
  as its sequence export (ADR 0171) names them: the type list says which
  fields an export writes from. Its features are then listed in a Features
  card, and their field says how many. The map links to where it's drawn in
  full: its viewer's tab, or its plugin's tab called Map.
- **The rail is 300 pixels wide,** its facts a label beside a value, and the
  long ones (how it was made, where it was imported from, its signatures) under
  their labels, so a run and its action read whole.
- **A record and its registry entry are one linked record,** listed once in the
  rail and counted once on the Lineage tab, with the reference fields that link
  them: "via Sequence". The Audit trail tab's count is the record's revisions
  and its entry's, and says so: on the tab, beside each list's heading, and in
  the rail's Revision.
- **Each revision exports as it was,** the latest too, from an icon button in
  its row.

**Administration has its own area,** with its own sidebar and a way back to
Iki. Its groups are:

- **Organization:** general settings, sign-in and security, people and
  teams, projects.
- **Data:** types and registries, plugins, settings for plugins.
- **Compute and cost:** compute, and costs and budgets.
- **Compliance:** the audit log, signature meanings, exports and backups.

Each page needs the permission it needs today.

- **The sidebar offers only what its reader may open.** A group with nothing in it is
  left out, and `/admin/` opens the first page they may open. Someone with no
  administrative permission at all gets no admin frame: a page of the area tells them
  it isn't theirs in the main one.
- **Every page of the area is under `/admin/`,** server-rendered or the web app's,
  named for what it is: `/admin/sign-in/`, `/admin/people/` (and `/admin/people/<id>/`),
  `/admin/service-accounts/`, `/admin/apps/`, `/admin/email/`, `/admin/registries/`,
  `/admin/plugins/`, `/admin/outside-apis/`, `/admin/webhooks/`, `/admin/compute/`
  (with `workers/`, `slurm/`, `quotas/` and `volumes/`), `/admin/costs/`,
  `/admin/audit/`, `/admin/signatures/` and `/admin/legal-holds/`. One prefix says
  which frame a page is in, and its address reads as its breadcrumb does. The old
  addresses, under `/settings/`, `/people/`, `/service-accounts/`, `/compute/`,
  `/costs/` and `/audit/`, are gone, without redirects: Iki isn't deployed anywhere
  before M1, so no link or bookmark points at them. The API's paths don't change.
- **The top bar holds a breadcrumb** of "Admin / page", and the page's own actions.
- **The groups hold the pages Iki has:**
  - Organization: sign-in and provisioning, people (with service accounts), apps and
    email.
  - Data: registries, plugins, the outside APIs plugins call, and webhooks.
  - Compute and cost: compute (overview, workers, Slurm clusters, quotas, volumes),
    and costs and budgets.
  - Compliance: the audit log, electronic signatures and their meanings, and legal
    holds.

  General settings, an admin list of projects and exports or backups have no page
  yet: each joins its group when it has one. Teams stay in the main sidebar, under
  People and teams, because everyone uses them, not only administrators.

- **`navigation.json` lists the sidebar,** with the `can` each item needs (any one of
  them), so the server's pages and the app's draw the same one.

**The look is compact.**

- **Type:** 13px Inter, self-hosted (SIL Open Font License), with monospace
  for IDs and sequences.
- **Sizes:** rows of 28–34px, and controls 28px high.
- **Color:** one teal accent, and neutral greys for surfaces.
- **Themes:** both light and dark, at WCAG 2.2 AA contrast.

No page loads fonts or scripts from outside the deployment.

### The sidebar, in detail

- **One description, two renderers.** `navigation.json` lists the sidebar's
  items, what each needs, the paths each is current on, and its icons. The
  app and the server draw from it, and tests check that both show the same
  items to the same people. The current item is the one whose path is the
  longest prefix of the page's: a project's pages mark the project, not All
  projects. Only that item has `aria-current="page"`.
- **Projects** shows the first eight active projects you belong to, by team
  and then name, and the one you are on if it is further down.
- **Saved views** are your own saved searches, of every project or of one
  you can still read, the first eight by name; those others share stay in
  their project's list. Each opens from its own address,
  `/saved-searches/<id>/`, on its project's table, or All entities for a
  search of every project, so a link to one keeps working as the table's
  address changes.
- **Entities stays open or closed** as each person left it. This is kept in
  a cookie, so the server draws it the same way without a flash. Its types
  are those the counts list, as All entities counts them; where counting
  would be slow they're listed without numbers, and if the counts can't be
  read, Entities holds All entities alone.
- **Admin** is one link, to `/admin/`, for whoever may open any page of the
  admin area. The area's own sidebar takes them through the rest, and its
  pages don't draw this sidebar.
- **Search everything**, the sidebar's button, opens the palette with an
  `iki:search` event, as ⌘K does. The palette binds the shortcut, once a
  page; the sidebar binds none of its own. A server page shows the button
  only where it has loaded the palette.
- **The account menu** shows your name and email, notification preferences,
  authorized apps and sign-out. Error pages keep the sidebar, so you can
  leave them by its links.
- **On a narrow screen** (below 896px) the drawer is a modal dialog. The page
  behind it can't be reached until it closes, by Escape, its close button,
  the backdrop or a link. Without JavaScript, the sidebar shows open above
  the page.
- **Sizes:** 1rem is 13px, so sizes in rem keep their proportions.
- **Borders:** inputs' borders have 3:1 contrast against the page, as WCAG
  1.4.11 asks of a control's edge. Borders that only divide are lighter.
- **The Inter subsets** are Latin, Latin Extended and Greek; other scripts
  fall back to the system's font.
- **Marks:** a project's mark in the sidebar is its color's square, worked
  out the same way by the server's pages and the app.

## Consequences

- People who used the header's links find them in the sidebar or under
  Admin, at the addresses under `/admin/`.
- **Saved searches across projects are allowed.** A saved search that names
  no project finds records in every project its reader can read. It is its
  owner's alone: with no project, there is no one to share it with.
- **Counts are cheap or absent.** The sidebar's type counts come from one
  grouped query over what the reader can read; each search finds its rows
  by its indexes (ADR 0103). A count that would be slow is left out, not
  estimated. Before counting, Postgres's plan estimates the rows it would
  read; past a million, every type is listed without a count. The search
  time limit backs the estimate up.
- **A row names the projects you can read.** Each record lists the projects
  it is in that its reader can read, its owner first, in the lists and in a
  download's Project column. A project they aren't in is never named.
- **Some things need a project.** An action runs in one project, so records
  picked across projects can be run on only in a project they are all in
  where the reader runs actions. People are picked from a project's members,
  so across projects a person field filters by "is me", set or empty.
- **A project's tabs are the same tables.** Its Entities, Requests and Runs
  tabs are the lists above, held to the project, so there is one table of
  each, not two. Its Entities tab has the same type pills as All entities,
  each counting the project's records, in place of a Type select.
- **Audit stays complete.** The audit trail tab reads the same history as
  before; only where it is shown changes. Part 11's requirement that the
  trail be available for review and copying (§11.10(b), (e)) is met by the
  tab and its export.
- **The export is the whole trail.** `GET /entities/{ref}/revisions.csv`
  writes one row per changed field, the record's revisions and then its
  entry's, with who, how, when (UTC), the reason, the signatures and any
  holds. It follows ADR 0175's CSV rules, including its guard against
  formulas, whatever the tab is filtered to, and each download is in the
  audit log. An entry's revision made through a project the reader can't
  read says what changed, never who or when, as on the tab.
- **Filtering by person reveals no one.** `GET /entities/{ref}/contributors`
  lists only the people whose revisions the reader is told about. On an
  entry's revisions, a person filter matches only those revisions, except
  for registrars, who see every revision.
