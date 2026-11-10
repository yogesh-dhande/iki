# 0031. Signed webhooks, sent by the task runner and governed by Org Admins

- Status: Accepted
- Date: 2026-11-10
- Refines: [0025](0025-append-only-audit-log-and-database-roles.md),
  [0028](0028-oauth-authorization-server.md),
  [0030](0030-tasks-live-updates-and-notifications.md)

## Context

Labs connect Iki to the systems around it, such as an ELN, a LIMS, a data
catalog or a chat bot. Those systems want to hear when something changes,
not poll for it. In this release that means projects, their members, teams
and people. Runs and entities come later.

Webhooks send data out of Iki. In regulated companies, someone has to be able
to say where that data goes, and stop it. The receivers are often inside the
company's own network, behind an internal CA. And a server that makes HTTP
requests to addresses its users type in is a classic way to reach things it
shouldn't (server-side request forgery, or SSRF).

## Decision

**We follow [Standard Webhooks](https://www.standardwebhooks.com/).**

- **Headers.** Each message carries `webhook-id`, `webhook-timestamp` and
  `webhook-signature`.
- **Signature.** It is `v1,` and the base64 HMAC-SHA256 of
  `id.timestamp.body`, under a 256-bit secret shown once as `whsec_…`.
- **Libraries.** Receivers verify with the standard's libraries, in most
  languages, rather than code of ours.
- **Body.** It is `{type, timestamp, data}`. `data` holds what changed, with
  before and after for updates, and who did it.
- **Redelivery.** Sending a message again keeps its `webhook-id`, so
  receivers can drop duplicates.

**Events come from the audit log.** Each audited change becomes an event in
the same transaction, by way of a listener on `audit.record()`. So a change
sends exactly the events it was audited with, and a rolled-back change sends
nothing. A new kind of change needs a builder before it reaches webhooks. The
catalog at `/api/v1/webhook-events` lists what each kind of webhook may
choose: project webhooks get their project's events, organization webhooks
get every event.

**Deliveries are tasks** (ADR 0030), one row per message.

- **Retries.** A failure is retried after 5 seconds, then 5 minutes,
  30 minutes, 2 hours, 5 hours, and 10 hours twice. That is 8 attempts over
  about a day.
- **Failing webhooks are turned off.** After 5 days of nothing but failures,
  Iki turns the webhook off and tells its managers. Events that happen while
  a webhook is off are not sent later.
- **What's recorded.** Each attempt records the status code, the error and
  the duration, never the response body. The log keeps 30 days, and
  deliveries still pending after 2 days are closed.
- **Managers can act on it.** They see each delivery's body and attempts,
  can send any delivery again, and can send a test message (`ping`).

**Sending is SSRF-safe**, the same way as fetching apps' metadata documents
(ADR 0028).

- **Addresses are checked once.** Iki resolves the host once, and refuses it
  unless every address is public. IPv4 addresses embedded in IPv6 ones are
  decoded and checked too.
- **It connects to what it checked.** Iki then connects to that checked
  address, with the real name for TLS (SNI) and `Host`. A DNS answer that
  changes between the check and the connection can't redirect it.
- **No redirects.** Each attempt has one 15-second deadline.
- **HTTPS only.** Certificates are verified. Plain `http://` is for
  development only (`IKI_WEBHOOK_ALLOW_HTTP`, refused while HTTPS is on).
- **Internal receivers are explicit.** Operators list them by network
  (`IKI_WEBHOOK_ALLOWED_NETWORKS`), and can add an internal CA
  (`IKI_WEBHOOK_CA_FILE`).
- **Only task runners send.** The chart's network policy lets only task
  runners reach receivers, on public addresses and port 443 by default.

**Who manages them follows GitHub.**

- **Who adds them.** Project Admins manage their project's webhooks. Org
  Admins manage the organization's.
- **Tokens need a scope.** A token needs the `webhooks` scope, like GitHub's
  `admin:*_hook`. The scope is never implied by `write`.
- **Secrets.** A secret is shown once, when it's made. It is stored encrypted
  with the other secrets (`IKI_ENCRYPTION_KEYS`). Rotating it signs with both
  the old and the new secret for 24 hours, so receivers switch without
  missing a message.
- **Archived projects.** Their webhooks can't be changed.

**Org Admins govern project webhooks.**

- **An inventory.** It lists every project's webhooks: the project, the
  host, the events, and who added it. It never shows the full address, which
  may carry the receiver's token, or the deliveries.
- **A switch.** Org Admins can turn any project webhook off, and only an Org
  Admin can turn it back on.
- **A policy.** It decides whether project Admins may add webhooks at all.
  With it off, project webhooks stop sending.
- **Everything is audited.** Adding, changing, deleting, rotating, turning
  off and redelivering are all in the audit log. The audit log records the
  host, not the address.

## Consequences

- Receivers verify with off-the-shelf libraries, and we don't invent a
  signature scheme.
- Webhooks can only say what the audit log says. That keeps them honest,
  but each new kind of change needs an event builder and a test.
- Delivery is at least once and in no guaranteed order. Receivers dedupe on
  `webhook-id` and order by `timestamp`, as the standard recommends.
- Internal receivers need an operator to allow their network, and their CA
  if it's private. A project Admin can't point Iki at the network on their
  own.
- The development cluster runs a receiver, and the end-to-end checks verify
  the signatures of what the task runner sent.
