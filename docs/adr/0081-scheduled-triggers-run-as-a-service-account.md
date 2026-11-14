# 0081. Scheduled triggers run as a service account

- Status: Accepted
- Date: 2026-11-13
- Refines: [0027](0027-rest-api-conventions-and-service-accounts.md),
  [0080](0080-triggers-run-actions-when-entities-change.md)

## Context

Some work isn't a reaction to a change. A nightly import from a public
database, a weekly sync with a LIMS, or a morning report runs on the
clock. GitHub Actions has `on: schedule`, and Slack's workflow builder has
scheduled workflows. Both use cron's five fields, and both run as whoever
set them up.

Scheduled work differs from an entity trigger in three ways:

- Nothing changed, so there is no entity to hand to the action.
- It keeps running long after anyone thinks about it. A run that names the
  Admin who turned it on years ago reads as if they did it. In regulated
  labs, standing jobs run under a named system account, so the record says
  "the nightly import did this", and a person stays accountable for it.
- A slow run can still be going when the next time comes round. Two copies
  of an import racing each other write duplicates.

## Decision

**A trigger can run on a schedule.** It uses ADR 0080's records, with
`on: schedule`:

- `schedule` is cron's five fields, read as POSIX cron and GitHub read them
  (names for months and weekdays, `7` for Sunday too, either day field
  matching when both are restricted).
- `timezone` is an IANA name, UTC if left out. A time skipped by a
  daylight-saving change doesn't run that day; one that happens twice runs
  once.
- A schedule runs at most every 5 minutes, as GitHub's.
- There is no `type`, `fields` or `input`. The action must need no input,
  so `params` give it all it takes.

Iki parses cron itself, in the spec package, so the build and the server
read schedules the same way and no dependency is needed.

**It runs as a service account, on an Admin's authority.** Turning a
scheduled trigger on, the Admin picks one of the project's service-account
members (ADR 0027) who can run the action's tier there. Its runs are
launched as that account, with `via=trigger`. The Admin stays responsible:

- they too must be able to run the tier, when they turn it on and each time
  it fires;
- runs on the container and VM tiers wait for their approval, not the
  account's;
- if either of them loses the role, Iki turns the trigger off and tells the
  project's Admins.

An Admin can turn it on again to pick another account, or take the
authority on as theirs. As with entity triggers, that happens only in the
web app.

**Each time runs at most once, and never on top of the last.** Turning it on
works out when it next runs. Each minute the task runner finds the
triggers that are due, records a firing for the time, and works out the
next one. Times missed while Iki was down run once, not once each. If the
last firing hasn't launched its run, or its run hasn't finished, the new
one is `skipped` (`still_running`), much as GitHub's `concurrency` keeps
one run of a group going at a time.

**Each run knows where the last left off.** Its `input.json` carries
`run.trigger`: the trigger's name, the time it was for (`scheduled_for`),
and the time the last successful run was for (`since`, null the first
time). An import fetches only what changed since then. A failed run doesn't
move `since` forward, so the next one picks up its share. Scheduled runs
never reuse earlier results (ADR 0057): the same inputs mean "fetch again".

## Consequences

- A scheduled run's record names the account that did it, the Admin who
  answers for it, the trigger and the time it was for.
- Imports that run on the clock don't overlap, and don't lose ground when a
  run fails.
- A project needs a service account before it can schedule anything. That
  is one more step, and the right one for standing work.
- An action that takes an entity can't be scheduled. Looping over entities
  on a schedule belongs in the action, or in a dynamic collection it reads.
