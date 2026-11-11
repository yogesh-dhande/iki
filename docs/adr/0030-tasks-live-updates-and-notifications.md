# 0030. Background tasks on Postgres, live updates over SSE, and notifications

- Status: Accepted
- Date: 2026-11-10
- Refines: [0004](0004-backend-stack-django.md),
  [0027](0027-rest-api-conventions-and-service-accounts.md),
  [0029](0029-react-web-app.md)

## Context

Iki now needs to tell people things: that they've been added to a project,
that an app awaits approval. Later it will need to tell them that a run has
finished. Some of this happens away from requests: sending email, calling
webhooks, and later, scheduling runs. Pages should update without a reload.

Pharma and life-science customers run Iki themselves, often in validated
(GxP) environments, and every component they run is one more to qualify,
patch and back up. They expect email to be encrypted. They expect a
notification never to reveal what the person can no longer see. And they
expect the audit trail to show who changed their own email choices.

## Decision

**Tasks run on Django's Tasks framework, stored in Postgres.** Code declares
work with `iki_server.tasks.task()` (Django's `@task`, plus `max_attempts`)
and calls `.enqueue()`. Our backend writes a row to `iki_task`, and
`iki-server tasks` runs it. There's no Redis, RabbitMQ or Celery: Postgres
is already backed up, monitored and qualified.

- **Enqueueing is transactional.** The task's row is written in the
  change's transaction, so it exists exactly when the change does. That's
  the transactional-outbox guarantee without a separate outbox.
- **Runners wake at once.** Enqueueing sends `NOTIFY iki_tasks`. Runners
  `LISTEN`, and also poll every few seconds, so a missed notification only
  delays a task.
- **Claims never collide.** Runners claim rows with `FOR UPDATE SKIP LOCKED`,
  so any number of runners share the queue, and the highest priority goes
  first.
- **At least once.** Each attempt holds a 60-second lease, renewed every 15
  seconds and checked with a fencing token. When a runner dies, the lease
  runs out and another runner retries the attempt. Its late result is
  ignored, because the token no longer matches. Tasks must therefore be
  idempotent: the email task, for one, records that it sent.
- **Retries back off.** Retries back off exponentially from 30 seconds,
  doubling to at most an hour, with jitter. A task can raise `Retry(after=…)`
  to choose its own delay, or `GiveUp` to fail at once (a 5xx from the mail
  server, say).
- **Only declared tasks run.** The runner imports a task by its path, and
  refuses anything at that path that isn't a declared task.
- **Runners stop gracefully.** On SIGTERM a runner takes no new tasks and
  gives running ones a grace period. It has its own liveness and readiness
  probes, and runs as its own Deployment under its own service account.

**Live updates are Server-Sent Events on `/api/v1/events`.** One async
view, served by the same uvicorn processes as the rest of Iki.

- **One database connection per process.** Changes call
  `pg_notify('iki_events', …)` in their transaction, so an event is only
  sent once the change commits. Each web process holds a single `LISTEN`
  and fans events out to its open streams. A stream holds no database
  connection while it waits.
- **Events carry identifiers, never content.** They say _that_ something
  changed, and each one is checked against the caller's access before it's
  sent. The client then reads the change through the API, with the API's
  authorization.
- **Credentials are checked again every 20 seconds.** Each heartbeat checks
  that the session or token is still valid. When it isn't (signed out,
  deactivated, or a token revoked), the stream sends `signed_out` and ends.
  Streams end after 30 minutes anyway, so they spread across pods. Holding a
  stream open isn't activity, so it doesn't extend the session's idle
  timeout (21 CFR Part 11 §11.10(d)).
- **Streams are limited.** A person can have 20 at once on each process, and
  a process holds at most 2000. Hidden browser tabs close theirs after five
  minutes.
- **A page can follow one thing.** `/api/v1/runs/{ref}/events` streams one
  run's changes (its status, attempts and progress, and how far its log has
  grown) to whoever can read it, checked at each event, so a run's page
  follows it as it runs. These streams count toward the same limits. Later
  things worth following get a stream of their own the same way.

We chose SSE over WebSockets because it's plain HTTP. It passes the proxies
and inspection appliances our customers run, it reconnects by itself, and
it needs no second protocol to secure. Live updates only flow from server to
browser; changes still go through the API.

**Notifications are rows, emails are tasks.** `notify()` runs in the
change's transaction. It writes one notification per recipient, publishes
the live event, and enqueues an email for those who want one.

- **Nobody is told about their own action.** Service accounts and
  deactivated people aren't told either. A notification links only to a
  path on this site, enforced by a database constraint.
- **What a person can't see, they can't read.** The inbox hides
  notifications about projects the person can no longer see. The one telling
  them they were removed stays. A token limited to some projects sees only
  notifications about those.
- **People choose email by kind.** The audit log records each change of
  choice. Org Admins can leave details out of emails, so they only say
  there's something new in Iki. Some organizations don't want project names
  in mail.
- **Email is encrypted, or explicitly not.** `IKI_EMAIL_URL` takes only
  `smtp+tls://` (STARTTLS) or `smtp+ssl://`, and verifies the server's
  certificate. Plain `smtp://` is refused while HTTPS is on, unless the
  operator says the relay is internal (`IKI_EMAIL_ALLOW_PLAINTEXT`). The
  ambiguous `smtps://` is refused.
- **Emails don't start loops.** They carry `Auto-Submitted: auto-generated`
  (RFC 3834) and Outlook's `X-Auto-Response-Suppress`, so out-of-office
  replies stay quiet. They also carry `List-Unsubscribe`, pointing at the
  person's email choices.
- **Only the pods that send mail can reach the mail server.** The chart lets
  web pods (for the Org Admin's test email) and task runners reach it, on
  public addresses by default. Nothing else can.

Signed webhooks come next. They follow the same pattern: deliveries are
tasks.

## Consequences

- There's no extra service to run, back up or qualify, and nothing is lost
  between a change and its consequences. Postgres takes the load. If a queue
  outgrows it, the Tasks framework lets us swap the backend without touching
  callers.
- Each web process keeps one more database connection, for `LISTEN`. Behind
  a proxy, `/api/v1/events` needs response buffering off (Iki sends
  `X-Accel-Buffering: no`) and idle reads of at least a minute.
- `NOTIFY` payloads must stay under 8000 bytes. Ours are ids, and
  `publish()` refuses anything larger.
- Tasks run at least once. Every task we write must say why running it
  twice is safe.
- The development cluster runs Mailpit, and the end-to-end checks read the
  email the task runner sent.
