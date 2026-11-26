# 0232. A service orders its own work from primitives

- Status: Accepted
- Date: 2026-11-25
- Refines: [0080](0080-triggers-run-actions-when-entities-change.md),
  [0081](0081-scheduled-triggers-run-as-a-service-account.md),
  [0132](0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md),
  [0037](0037-entity-search.md)

## Context

A core facility decides which request to work next. Usually a coordinator decides,
case by case. Some cores want it to follow a rule they keep themselves. A sequencing
core can rank the programmes it serves, so oncology discovery comes before methods
development, and sort its queue by that rank, then urgency, then the date each
request is needed.

iLab, CrossLab and Jira Service Management leave priority to the service. Jira computes
it with automation rules that the service's administrators write. GitHub Projects sort
by any field, with a further sort for ties. None of them builds a "project rank" into
the product. Each core's rule is its own and changes.

Iki should not build one either. A built-in ranking would be a policy that most cores
don't follow, and one that changes with every portfolio review. Iki should give a lab the
primitives to keep its own rule. Before this, four were missing:

- A run could not see which project sent a request. The service's people see it on
  the request's page, but the run input gave only the request's values.
- A lab's request type could not add a field for the service's own use, such as a
  rank. A subtype of a request type couldn't declare `request` at all.
- Nothing fired in the service's project when a request arrived. A request is the
  asking project's record. Triggers fire in a record's own project, and sending a
  request makes no revision.
- The queue sorted by one field at a time. It couldn't sort by a project's side of
  the request. It sorted enums such as priority alphabetically, not in their order.

There was also nowhere for the rule to read its own data. An action run by a trigger is
given the entity that fired it, and what's related to it in the graph. A core's ranking of
the projects it serves isn't related to a request.

## Decision

**A run sees a request's two sides.** Each request in a run's input has `request`: the
asking project and the service, each by ID, slug and name. The service is null until the
request is sent. ADR 0132 already shows each side's people the other side's name, so a
run learns nothing new. A project's slug never changes, so a lab's records match projects
by slug. Action tests give it as `request: { asking_project: … }`.

**A lab's request type adds its own service fields.** A subtype of a request type can
declare `request` with only `service_fields`. These must be its own fields. The service
writes them along with its parent's service fields. Everything ADR 0132 says about service
fields applies, and the database's guard covers the subtype's table too. The asking
project can't write these fields. It sees them on its own request, as it sees every
service field.

**A request's arrival fires the service's triggers.** `on: request.received` fires once,
in the service's project, as a request of the trigger's type is sent there. Its action
runs in the service's project, so it writes the service's fields, as any run there does.
It doesn't fire in the asking project. Nobody in the service's project sent the request,
so its runs are launched as a service account. The Admin who turns the trigger on chooses
the account, and the runs are on that Admin's authority, as a schedule's are (ADR 0081).
Each run's record names the account, the Admin, the trigger and the request. ADR 0080's
other rules still hold: it's off until an Admin turns it on, only in the web app, and what
a triggered run writes fires nothing.

**An action can read its project's own records of a type.** An action's input can be
`from_project: true`. It isn't chosen: every run is given every record of the input's
type that its project owns and hasn't archived. A core's ranking of the projects it
serves is one example; a price list or a plate layout are others. The records are pinned
at their current revisions, like any input, so the run's record shows the ranking it
read, and identical rankings reuse results (ADR 0057). The input can't take more than its
`max`, and never more than 1,000. A launch, trigger or workflow step can't choose these
records. The input takes only records the project owns. It never takes records shared
into the project, or requests sent to it.

**The queue sorts by several fields, and by either side's project.** A search's `sort`
can be followed by up to three `then` sorts for rows that tie. Blanks come last in each
sort. Pages are still keyset pages. Requests also sort by `asked_by` and `sent_to`, by
the project's name. That name is already visible to both sides; other records leave the
column blank. An enum, or a state field's states, sorts in the order its type declares
them. Priority sorts routine before urgent, so the descending order puts urgent first.
Saved searches, the CSV download, the API and the MCP search tool take `then`. The grid
puts each further sort in a "Then by" menu.

`examples/plugins/request_priority` uses the new primitives. A sequencing core keeps a
Project served record for each project, with a rank its coordinators agree. Agreeing a
rank locks it, and changing it returns it to Proposed, which only a coordinator can set.
A Sequencing request extends Plain request with a `project_rank` service field. When a
request arrives, a trigger runs `rank_requests`. The action gives the request the agreed
rank of the project that asked. The queue sorts by project rank, then priority
(descending), then needed by.

## Consequences

- A core that prioritises by hand needs nothing from this: priority stays a service field
  its coordinators set. A core that automates it writes the rule as its own action. It
  keeps the rule's data as its own records, and turns on the trigger it wants.
- Re-ranking after the ranking changes is a run the coordinators launch. They filter the
  queue to open requests, select them and run `rank_requests` again. The arrival trigger
  can't redo it, because a ranking's change isn't a request's. A trigger on a ranking's
  change can't find the open requests either. They aren't related to it, and they aren't
  the project's own records.
- `from_project` reads only a project's own records, so it can't be used to read another
  project's data through a share. It also can't read the requests a service was sent.
- A further sort or a sort by `asked_by` or `sent_to` can't use an index. Like any
  unindexed sort, it is refused above a million rows (ADR 0103). A queue holds far fewer
  rows than that.
- Rules and guards (CEL) still can't read a request's sides. A rule that names projects
  would put a lab's own projects into its plugin, which is the kind of built-in policy
  this ADR avoids. A rule can read a service field that the lab's action fills from its
  records.

## Alternatives considered

- **A ranking setting on the service's intake.** It's simple, but it's one policy, and it
  belongs to Iki. A core that ranks by programme value, contract or due date would need
  another setting each time.
- **Plugin settings for the ranking.** A deployment sets plugin settings in its config
  repository. A core's coordinators can't change them, and they have no history of their
  own.
- **Running the arrival trigger as the Admin, as entity triggers do.** The run's record
  would say that an Admin ranked every request, for years after they turned the trigger on.
  The request came from outside the project, as a schedule's time does, so a named
  account runs it.
- **A trigger key that searches the project for an input's records.** It's more general,
  but it would need a second condition language in trigger manifests. An action input is
  simpler, and it works the same when a person launches the action, a workflow runs it or
  a trigger runs it.
