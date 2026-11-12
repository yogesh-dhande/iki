# 0072. An agent's changes name the person and the agent

- Status: Accepted
- Date: 2026-11-12
- Refines: [0025](0025-append-only-audit-log-and-database-roles.md),
  [0013](0013-agents-via-mcp-oauth-approvals.md),
  [0028](0028-oauth-authorization-server.md),
  [0050](0050-agents-runs-on-costly-tiers-wait-for-approval.md)

## Context

An agent works for a person, through an OAuth client the person allowed.
Regulated labs must be able to say who is responsible for each change and
what made it (21 CFR Part 11, EU Annex 11). For an agent, the honest answer
names both: the person, and the agent acting for them. GitHub records
"user via app" for a GitHub App acting for someone, and RFC 8693 gives a
token both a subject and an actor (`act`).

Until now Iki recorded the person, and the client as a label such as
"Claude Code (4f2a…)". The label can't be filtered on reliably. It doesn't
say which of the person's grants was used, or which agent conversation made
the change. Files didn't record a client at all.

## Decision

**Changes made with a token name the person and the agent.** The person
(the principal) stays where it was on each record: who requested a run, who
made a revision, who added a file, who acted in an audit event. Beside it,
the record now points to **the agent** (the actor), which is:

- the OAuth **client ID**, and the client's name as it was then;
- the **grant**: the person's consent to that client (ADR 0028). Each
  consent has a grant ID. It keeps it while the person narrows or widens
  it, and a consent given again after being revoked is a new grant;
- the **MCP session** the request belonged to, if it came through MCP;
- what the agent **says** about itself: its software (MCP's `clientInfo`
  name and version) and its model (the `Iki-Agent-Model` header, such as
  `claude-opus-5-5`). These are what the agent reports. Iki can't check
  them, and labels them so wherever they're shown.

Each distinct agent is stored once, append-only, like the audit log
(ADR 0025). Audit events, runs, batches, run approvals, revisions, files,
uploads and registered files point to it. Changes made in the web app, by
the CLI's commands on the server, and by the system point to none.

**MCP sessions.** Clients on MCP versions before 2026-07-28 start with
`initialize`. Iki answers it with an `Mcp-Session-Id` that it signs. The ID
carries the session and the `clientInfo` the client gave, and is bound to
the token's client and person. Iki keeps no state for it, so any replica
can check it. A session ID that fails the check, or that belongs to another
client or person, is answered `404`, and MCP clients then start a new
session. 2026-07-28 has no sessions: each request carries its
`clientInfo`, which Iki records, and has no session to record.

**Everywhere it's shown, it says both.** "Ada Lovelace, through Claude
Code" on runs, batches, approvals, revisions and files. The details show
the client ID, the grant, the session, and what the agent says it is. The
API gives each of these records an `agent`, and webhooks' events carry it.

**The audit log filters by agent and by grant.** Auditors find everything
a client did, or everything one grant did. A person's **Account → Apps**
links each of their grants to what it did.

**Older records keep their label.** They were recorded as "Name (client
ID)". Where the client ID can be read from the label, the API gives it as
the agent's, with no grant or session, and the audit log's filters find
them. Append-only records aren't rewritten.

**Approvals stay human.** Deciding on an agent's request for runs (ADR 0050) still takes the web app. No token can, whoever's it is.

**Unattended agents** acting for a person, through RFC 8693 token exchange
with the `act` claim, come next, in their own ADR.
They're recorded the same way, with their delegation as the grant.

## Consequences

- Positive: a lab can say, for every change, which person is responsible
  and which agent made it, down to the grant and the conversation.
- Positive: revoking a grant, then reading everything it did, is one
  filter.
- Positive: one stored agent per combination keeps records small, and
  can't be changed after the fact.
- Negative: an agent's software and model are what it says. A dishonest
  client can claim to be anything. The client ID and grant are what Iki
  vouches for.
- Negative: on MCP 2026-07-28 there's no session to group a conversation's
  changes by.
