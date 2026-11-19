# 0142. A request tells its service, and the project that asked

- Status: Accepted
- Date: 2026-11-18
- Refines: [0129](0129-types-declare-their-notifications.md),
  [0132](0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md)

## Context

A request goes from one project to another's service (ADR 0132), and two
moments in its life matter to people who weren't there: the service's
people need to hear that one has arrived, and the project that asked
needs to hear that its results are back. Iki audited both
(`request.received`, `request.result_released`) but told nobody.

A type's declared notifications (ADR 0129) couldn't say either. They're
sent when a record is made or a field changes, and tell people in the
record's own project. Sending a request changes none of its fields, and
it's made in the asking project, where the service's people aren't. A
result is released by a state change on the result, which is the
service's record, not the request's, so the asking project's people
aren't in its project either.

GitHub tells a repository's watchers when an issue is opened, and the
issue's author and subscribers when it's closed. ServiceNow and Jira
Service Management tell an assignment group when a ticket lands in its
queue, and the requester when it's resolved. Who is told is the
workflow's choice, not Iki's: one core's coordinators triage requests,
and another core's QA does.

## Decision

**A request type declares both, as it declares any notification**, with
two more events for `when`:

```yaml
notifications:
  received:
    label: A request arrives
    when: { sent: true }
    tell: { roles: [coordinator] }
    message:
      title: "{{ hrid }} arrived: {{ title }}"
  results_released:
    label: Its results are released
    when: { released: true }
    tell: { creator: true, watchers: true }
```

- **`sent: true`** fires once, when the request is sent to a service and
  linked into it. It tells holders of the named lab roles **in the
  service's project**, and only roles: the asking project knows it sent
  it, and its fields, creator and watchers are the asking side's. The
  validator refuses `fields`, `creator`, `watchers` or `actor` beside it.
- **`released: true`** fires as each result of the request is released
  back to the project that asked. It tells whom it names of the request
  (its fields, its project's role holders, its creator, its watchers) in
  the asking project. The notification is about the request and names
  the result, so each person is told only if they can read both.
- Either is a request type's: on any other type, the validator says so.
  Neither combines with `created`, `field`, `to` or `from`.

Everything else is ADR 0129's: who can be told and how, value-free
email, folding, people's settings, `required`, and the
`notification.sent` audit event. A request sent makes no revision, so
its event has none; it names the agent that sent it, if one did.

## Consequences

- A service's people hear of a request where they work it, and the
  project that asked hears of its results without watching for them.
- Iki adds no kinds of its own for requests: a plugin that declares
  neither tells nobody, as before, and people turn each off in their
  settings unless it's `required`.
- `iki/example_lims` tells a core's coordinators of each analysis
  request, and its maker and watchers of each result released.
