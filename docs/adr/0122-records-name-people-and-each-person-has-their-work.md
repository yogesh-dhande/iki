# 0122. Records name people, and each person has their work

- Status: Accepted
- Date: 2026-11-18
- Refines: [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md),
  [0118](0118-a-record-moves-through-its-types-states.md),
  [0120](0120-a-state-can-lock-a-record-and-the-database-holds-the-lock.md),
  [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0037](0037-entity-search.md),
  [0030](0030-tasks-live-updates-and-notifications.md),
  [0031](0031-signed-webhooks.md),
  [0026](0026-saml-and-scim-provisioning.md)

## Context

A lab's records say who does what: a sample has its analyst, its reviewer
and the QA who releases it, and each of them wants the list of what's
theirs now. LabWare and LIMS like it assign samples and tests to analysts
and give each a worklist; Benchling has assignees on requests and entries;
GitHub and Linear assign issues to people, and list "assigned to me". In a
GxP lab who was assigned a record is part of the record: it's in the audit
trail, it survives people leaving, and only someone qualified (a role) can
be named. Iki had lab roles (ADR 0116) and states (ADR 0118), but no way
for a record to name a person.

## Decision

**A field can name people.** `type: person` names one person, and a list
with `items: { type: person }` several (at most 50, each once). The value
is the person's user ID, a foreign key to `iki_user`, which never loses a
row: people are deactivated, not deleted. `role:` limits it to holders of
a lab role. The API takes an ID or an email address, and gives back IDs,
with each record's `people`: each person it names, their name, whether
they're active, and their email only to those who could see it on the
project's Members page (the person, or colleagues in the record's
project). A person field can't be a registry's (`person-in-registry`):
who tests something is each project's record, not every project's.

**Only someone who could do the work can be named**, by any path (web,
API, agents, runs' results, bulk imports): a person, not a service
account or a bundle's placeholder; active; able to read the record's
project, by a role of their own or a team's; and holding the field's lab
role there. A refusal reads the same whether or not the person exists
elsewhere, so naming isn't a way to probe the directory, and the
picker (`GET /projects/{id}/people`) lists only the project's people.

**Fields name people, not teams.** GitHub, Linear, Jira and Benchling
assign people; a team is who may take work, not who did it, and a GxP
record says which analyst tested a sample. Teams count where they
already do: a team's project role and lab roles make its members
nameable. What a lab means by "assign it to the analysts" is open work:
records whose assignment field names nobody, open to everyone holding the
field's lab role, who can take one (`POST /entities/{ref}/take`).

**An assignment field assigns the record** to whomever it names:
`assignment: true`, or `assignment: { states: [in_review] }` for the
states it's their work in, so a sample's reviewer has it only while it's
in review. A type can name `due:` a date field, which work sorts by.
Each change to an assignment field is part of the record's revision, and
is audited as `entity.assigned` (who was added and taken off, the
revision, the reason), fires the `entity.assigned` webhook and event, and
tells each person in Iki and by email: assigned, taken off, and "it's
ready for you" when the record's state becomes one that's theirs. A
change that names many people, or many records, tells each person once.
Who's named is an ordinary value, set by an edit like any other: changing
state doesn't name anyone, so naming people doesn't depend on how a type
moves between its states.

**A locked record's people are locked with it** (ADR 0120). Its person
fields are values like any other: an edit, a take, a bulk reassignment, a
run or an import that would change them is refused or skipped, and the
database's lock trigger guards their list tables too. So whoever has a
record in a locking state is named before it gets there (example_lims
names a sample's QA while it's in review), and changing them means
taking it out of that state first, which the record's history shows.
Keeping reassignment open on a locked record was the alternative; we
didn't take it. Who reviewed or
released a sample is part of what was approved (LabWare freezes a
sample's analyst and reviewer when it's authorized; Benchling locks a
signed entry's fields, assignees included), and a lock that let some
values change would be a weaker lock for every plugin. Work on a locked
record nobody is named for is still reachable: it's open work for the
field's lab role, which its holders do without taking it.

**My work** (`GET /me/work`, the web app's My work page, MCP's read-only
`my_work`, and both SDKs) lists each record whose assignment field names
the caller and counts in its state, across their projects, filtered by
project, type and state, soonest due first or latest changed. It's only
ever the caller's own. Open work (`/me/work/open`) lists what their lab
roles could take. Search finds `assigned` (`{{ me }}` is whoever
searches), `needs_reassigning`, and a person field's value, and the
grid's search box takes `assigned:me`, `assigned:none`,
`assigned:@ana@example.com`, `needs:reassigning` and `reviewer:me`. Person
fields aren't sortable: names aren't stored in the records' tables, and
sorting by an ID would mislead.

**When people leave, records keep them.** Deactivation, deprovisioning,
removal from the project (or a team's), and losing the lab role don't
change a record: its history says who was assigned. The person drops off
My work at once, since work is computed from who can be named now, and
the record `needs_reassigning`, which its page, the grid and search show.
Each of the project's Admins is told, once per departure, how many of
their records need reassigning and why. Reassigning is an edit (out of
a locking state, for a locked record); many at once from the grid
(`POST /entities/assign`) needs Editor, changes each record as its own
revision under one audited group, and skips, with why, what can't change.

**No crossing projects.** Eligibility is per record's project; search
and work read only what the caller can read; another project's people
can't be named, listed, or found by their email.

## Consequences

- A record's assignees are in its revisions, audit trail and webhooks
  like any value, and a bundle carries them: the same person by email
  where they're here, or a placeholder, whom the record needs
  reassigning from.
- An assignment can name someone who can't do the work any more; that's
  shown, not hidden, and fixed by a person, with a reason.
- My work is computed by querying each assigned type's table, merged and
  paged with a signed cursor: no denormalized worklist to drift.
- An agent reads its person's work but assigns only by editing records,
  as the person would.
