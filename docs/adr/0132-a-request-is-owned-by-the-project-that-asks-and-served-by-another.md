# 0132. A request is owned by the project that asks, and served by another

- Status: Accepted
- Date: 2026-11-18
- Refines: [0067](0067-a-project-can-keep-its-entities-to-itself.md),
  [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0122](0122-records-name-people-and-each-person-has-their-work.md),
  [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
  [0120](0120-a-state-can-lock-a-record-and-the-database-holds-the-lock.md)

Updated by [ADR 0211](0211-lots-inventory-samples-and-requests.md). The shipping Request and Deliverable families and the private WorkItem now live in `iki/requests`. Each returned record references its matching concrete request.

## Context

A lab rarely does all its own work. A discovery team sends samples to an
analytical core for purity and mass, to a sequencing core, or to a
biophysics group; the core works a queue of such requests from many teams,
and sends results back. iLab and CrossLab run core facilities this way,
Benchling Requests runs it inside one tenant, and Jira Service Management
splits a request between the customer who raised it and the agents who work
it. GitHub's cross-repository issues show how two owners link one thing
without either losing control of their own.

Until now an Iki record had one owner and one set of writers: the Editors
of its project. Sharing (a link into another project) gives read access,
and nothing else. A request needs two writers, each for its own part: the
asking project says what it wants, the service says where the work is and
who does it. Each side must see only what the request gives it: the core
mustn't see the asking project's other data, and the asking project
mustn't see the core's work in progress, or other teams' requests.

What a request's workflow is (its states, who sets each, what guards them)
differs between labs, so it belongs to plugins (ADR 0125). What core must
own is the primitive: who owns a request, who may write which part of it,
what it carries across, and what comes back.

## Decision

**A type says its records are requests.** A plugin's entity type may have
`request: { service_fields: [...], sends: [...] }`. `service_fields` are
its own fields the service writes, its state fields among them; every other
field is the asking project's. `sends` names its references (or lists of
them) whose records go to the service with it, such as its samples. A
type with `result: { of, released: { field, states } }` is a request's
results: `of` is its required, immutable reference to the request, and
`released` the state field, and states, that send it back. The validator
keeps these sound: a service field can't be a reference, a file, unique,
immutable, or required without a default; `sends` can't name a service
field; a request or a result has no identity (ADR 0115), so each is one
project's record; a subtype inherits `request`, and can't change it.

**The asking project owns it.** A request is made in the asking project, by
its Editors, as any record. Until it's sent, its service fields keep their
defaults and its service states their initial ones: nobody sets them. So
its pages say a service state is not sent yet, rather than show its
initial state, whose name and description (Submitted, "Sent, and waiting
for the core") would say it had been sent; a new request's form says the
service sets them once it's sent. The API still gives the initial state,
so a search by it finds unsent requests too, and `request.submitted` says
which are sent. This holds for every request type, without each plugin
declaring a draft state of its own.
Sending it (`POST /entities/{ref}/submit`) picks one service, for good, and
links the request, and each record its `sends` fields name, into the
service's project. For a reference to a registry entry, the asking
project's own record of it goes. Records named later go when they're
named; one taken out of the request stays with the service until the
service takes it out too, and the request itself stays there for good:
it's the service's record of what it was asked.

**A service says what it takes.** A project's Admins choose, under its
settings, the request types it takes (a parent type takes its subtypes)
and from whom: every project in its registry, or the projects they name
from those they can see. This follows Jira Service Management and iLab,
where the service's administrators configure intake, and the people asking
pick from the services offered to them. A project that isn't a service the
request can go to answers 404, worded the same whether it exists or not,
so a project's name doesn't leak to those it doesn't serve. Requests stay
within a registry: a project in another one is never offered.

**Each side writes only its own fields.** The service's Editors write its
fields, and set its state fields with the lab roles they hold in the
service's project (ADR 0116); the asking project's Editors write the rest,
and set its state fields with roles held there. Someone in both writes
both. Person fields among the service's fields name the service's people,
and its assignment fields put a request on their My work (ADR 0122). This
holds on every path: the API, a run (which writes as its own project's
side alone), an import, a bulk reassignment, a signature (ADR 0126). The
database holds it too: a guard on each request type's tables refuses an
update that changes the other side's columns, by the side each write says
it is (`iki.request_side`, the asking project's when unset), as ADR 0120's
lock guard does. A state that locks a request (ADR 0120) locks it for both.
Archiving it is the asking project's.

**Sharing out needs both sides' consent to share.** Sending a request
shares records, so a project that doesn't share its entities (ADR 0067)
can't send requests, and a sent record must be one the asking project may
share into the service's. What a request brought into a project goes no
further: it can be read and referenced by records in the project that received
it. It cannot be linked to a third project or referenced by records there.

**Work items stay in the service project.** A type can declare
`work_item: { of: request }`, naming a required, immutable reference to a
request. The service project creates them against a request sent to it.
Only that project can keep or reference them, including when a person belongs
to both projects. Work items have no result or release policy.

**Results are the service's until it releases them.** A request's results
are made in the service's project, which owns them, and reviewed there
with its own states: the asking project can't see them yet. When the
service sets one to a released state, it's linked into the asking
project, audited as `request.result_released`, and it stays there, as
released results must. A service that doesn't share its entities can't
release them.

**Each side sees only what the request gives it.** The service reads the
request and what it sent; the asking project reads its own records and the
results released to it. This is fuzzed both ways, as ADR 0101 fuzzes
authorization: of every record either project holds, each side's people
read, list and search exactly those.

**The queue is the requests sent to a project.** They're linked into it,
so they're in its grid and search, and a built-in `sent_to` condition
finds those sent to a project, or not sent at all. A project's Requests
tab, as GitHub's Issues tab, is its queue as a service, filtered by status,
assignee and the project that asked, and the requests it sent, filtered by
status and service. Each side lists the other by name alone (the projects
that sent it requests, the services it sent to), so it can filter by them
without seeing anything else of theirs. Each request's page
shows both sides: who asked, who serves it, whose each field is, what it
sent and what came back. Audited as `request.submitted` in the asking
project and `request.received` in the service's, each told to webhooks;
changes to what a project takes as `project.intake_changed`.

## Consequences

- A plugin defines a request's lifecycle as any other (ADR 0125): the
  example LIMS plugin has an analysis request (Submitted, Accepted, In
  progress, Delivered or Declined, set by the core's Coordinators, Analysts
  and Reviewers or QA, and Withdrawn by the asking project's Coordinators),
  and analysis results its QA releases, signing for each.
- A request can't be recalled once it's sent, only withdrawn through its
  type's states: the service keeps its record of what it was asked, as a
  regulated core must.
- A service sees the people the request names, and the asking project sees
  the service's, as the record names them; neither sees the other's
  members otherwise.
- Bundles (ADR 0096) carry a request as a record of the project exporting
  it; it arrives unsent.
- Telling the service's people of a new request belongs to notifications
  (step 60): for now webhooks and the audit log carry it.
- Not done: asking a service for access (intake is the service's choice
  alone), sending one request to several services, and requests between
  registries or deployments.

## Alternatives considered

- **The service owns the request.** iLab does this, but then the asking
  project's own record of what it asked lives elsewhere, can be changed by
  the core, and leaves with it. Owning it where it's asked, as Benchling
  and GitHub do, keeps each side's record its own.
- **A copy on each side, kept in sync.** Two records diverge, and neither
  is the record of what was asked; one record with two writers can't.
- **Field-level permissions in general.** A request is the case that needs
  two writers; a general per-field ACL would be harder to reason about and
  to audit, and the database couldn't hold it as simply.
- **Core states for requests.** Every lab's request lifecycle differs;
  plugins already define states (ADR 0125), so core keeps to the primitive.
