# 0129. Types declare their notifications

- Status: Accepted
- Date: 2026-11-18
- Refines: [0030](0030-tasks-live-updates-and-notifications.md),
  [0122](0122-records-name-people-and-each-person-has-their-work.md),
  [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
  [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md),
  [0072](0072-agents-changes-name-the-person-and-the-agent.md)

## Context

A lab's workflow tells people when something is theirs to act on: QA
hears a sample is in review, an analyst that QA rejected theirs, the
person who booked an instrument that it broke. LabWare and LIMS like it
send these from the workflow's configuration; ServiceNow and Jira have
notification schemes, events and recipients chosen per project, some of
them mandatory; GitHub and Linear notify participants and watchers, and
let each person subscribe to or mute an issue. Iki told people of its own
events (ADR 0030) and of work assigned to them (ADR 0122), but a plugin
couldn't say who hears of what in its workflow.

In a GxP lab who was told what is evidence ("was QA told?"), and a
notification is also a way to leak: an email lands in an inbox Iki no
longer controls, and a person might be told of a record they can't read.

## Decision

**A type declares its notifications**, under `notifications:` in its
manifest, each by name. Iki owns the machinery (who can be told, how, how
often, what's recorded); the plugin owns the policy (when, and whom):

```yaml
notifications:
  in_review:
    label: Submitted for review
    description: QA hears when an analyst submits a sample's results.
    when: { field: status, to: in_review }
    tell: { roles: [qa], watchers: true }
    message:
      title: "{{ hrid }} is in review"
      body: "{{ protein }}, lot {{ lot or 'not given' }}."
```

- **When**: `created: true` (a record is made), or `field:` changing, with
  `to:` and `from:` (a value or a list) for state, enum and bool fields. A
  field can be one of a referenced record's (`instrument.service`): it's
  declared on the referring type, and each record referring to the changed
  one (up to 1000, the newest; an entry's records in its project only) is
  told of. `where:` narrows it with a search's conditions (ADR 0037) on the
  type's own fields, where `{{ now }}` stands for the moment it's sent, as
  it now may in any search: "bookings that haven't ended".
- **Whom** (`tell:`, at least one): the people its person `fields` name,
  everyone holding one of its lab `roles` in the record's project, its
  `creator`, its `watchers`, and the person who made the change (`actor`,
  off by default). Registry fields can't send one: an entry has no project
  to tell.
- **What**: `message.title` and `body` use the display title's template
  language (strings, field names, `or`), nothing more: no expressions, no
  HTML, no loops. A state shows its label, a person their name, a reference
  its HRID only to someone who can read the referenced record. Without a
  message it reads "SAM-000012: Submitted for review".
- `email: false` makes it in Iki only by default; `required: true` means
  nobody can turn it off in Iki (below).
- The validator checks every name, value, role, operator and template, with
  hints, and warns (`notification-repeats-ready`) when one would repeat
  "It's ready for you" (ADR 0122) to an assignment field's person.

**Whom it tells is checked as it's sent.** A person is told only if they
are active, can sign in, and can read the record (and, for a referenced
field, the record that changed). Who can read it is asked of everyone
named at once, in the same few queries for a role of five or of two
hundred, as is who can read each record its message refers to. The inbox
and the email task check again: a notification someone can no longer read
disappears from their inbox, API included, and isn't emailed. Service
accounts are never told.

**Not of your own change, unless it wasn't by hand.** The person who
changes a record isn't told of their own change. A change an agent made for
them (ADR 0072), or a run or a trigger, does tell them, saying who did it:
"Ada Lovelace, through Claude Code", in Iki and by email.

**Emails carry no values.** A declared notification's email says what
happened (its label: "SAM-000012: Submitted for review"), its description,
the project, who did it, and a link, but never the record's values or the
message's body, which are shown in Iki to someone who can read them. The
organization's "say what it's about" setting (ADR 0030) still applies.

**Many at once is one notification.** A notification about records joins
the person's unread, not yet emailed notification of the same kind in the
same project from the last ten minutes: "3 Samples: Submitted for review",
which lists the records and links to them. Its email waits two minutes
(`IKI_NOTIFICATION_BATCH_SECONDS`) for others to join it. Reassigning 500
samples, or 500 going into review, is one notification and one email per
person. Assignments (ADR 0122) share this machinery. Bulk imports
(`quietly()`) tell nobody record by record, as with triggers.

**Each person chooses how they're told**, per kind: in Iki and by email, in
Iki only, or (a type's notification only) off, everywhere or in one
project, which overrides their choice everywhere there. Iki's own kinds
are about the person themselves and can't be off. Settings are under
Account → Notifications; a project's page has its own dialog. Each change
is audited.

**Required exists**, as ServiceNow's mandatory notifications do: a GxP
workflow must be able to say "the analyst is always told of a rejection".
A required notification can't be turned off in Iki, by kind, by project,
or by muting the record; whether it's also emailed stays the person's
choice. Required doesn't widen who's told: it still needs them to read it.

**Records can be watched or muted**, as GitHub's issue subscriptions and
Jira's watchers: watching tells the person what the type tells
`watchers`; muting tells them nothing of it but what's required. Watching
is offered only if a notification tells watchers, muting only if one isn't
required. Each choice is audited, and needs reading the record.

**Each one sent is recorded.** `notification.sent` is audited per record
it fired for, naming who was told and how (field, role, creator, watcher,
actor; by email or not), and who wasn't and why (deactivated, can't read
it, made the change, muted it, turned it off): up to 500 of each. Each
declared notification's email is audited too (`notification.emailed`). "Was
QA told?" is in the record's audit log.

**"It's ready for you" stays Iki's own**: it follows assignment (ADR 0122),
which core owns, and every assignment field has it. It's not a default
declared notification; it shares the delivery machinery (folding,
attribution, choices).

The REST API has `GET`/`PATCH /me/notification-settings` (replacing
`/me/notification-email`), `GET`/`PUT /entities/{ref}/watch`, each
notification's `kind_label`, `count`, `records` and `agent`, and each type's
`notifications`, as its page previews them. The Python SDK has
`iki.notifications`. The MCP server doesn't: as with approvals, how a
person is told is theirs to choose, not their agent's.

## Consequences

- A plugin's workflow says who hears of what, and Iki makes it safe:
  nobody is told what they can't read, emails don't carry data, and floods
  are folded.
- An inbox can hold one notification about many records; clients read its
  `records`, not only its `path`.
- The email preference table is replaced by notification settings, with
  three choices instead of two.
- Not done: digests (daily summaries), notifications on a schedule (an
  overdue sample), and other channels (Slack, Teams), which a plugin's
  webhook (ADR 0031) can reach meanwhile.
