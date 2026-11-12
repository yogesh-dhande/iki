# 0073. Unattended agents act for a person by delegation

- Status: Accepted
- Date: 2026-11-12
- Refines: [0028](0028-oauth-authorization-server.md),
  [0072](0072-agents-changes-name-the-person-and-the-agent.md)

## Context

An agent a person talks to signs in as that person (ADR 0028, ADR 0072).
Some agents run unattended: a nightly triage bot, a LIMS sync, an agent in
a pipeline. Today they use a service account's own credential, so what they
do is the service account's. Nobody is responsible for it but the service
account. Labs want such work to be a person's, done by an agent they chose
to let act for them, as far as they allowed, and for a limited time.

RFC 8693 (OAuth 2.0 Token Exchange) is the standard for this. A client
exchanges a token for one that acts for a subject, with an `act` claim
naming the actor. Google's service-account impersonation and Azure's
on-behalf-of flow work this way.

## Decision

**A person delegates to a service account's credential.** On **Account →
Apps**, a person lets a credential act for them:

- in projects they choose, or all of theirs;
- with scopes they choose, within the credential's own and never `admin`;
- until a date, at most 90 days away.

Only credentials an Org Admin has allowed to act for people can be chosen.
The default is off, because a credential's holder can then act as anyone
who delegates to it. The delegation is a grant, like a consent: it has a
grant ID (ADR 0072), is listed with the person's other apps, and is revoked
the same way. Giving, changing and revoking it is audited.

**The credential exchanges for the person's token.** At `/oauth/token/`,
`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, authenticated
as the credential, with:

- `subject_token`: the grant ID;
- `subject_token_type`: `urn:ikisci:params:oauth:token-type:delegation`;
- `scope`, optionally narrowing.

Iki answers with an access token for the person (`issued_token_type`
`urn:ietf:params:oauth:token-type:access_token`, no refresh token). The
answer carries `act: {"sub": "<client ID>"}`, the actor, as RFC 8693 puts
it. Exchanging again gets another token, until the delegation expires or is
revoked. `actor_token` isn't taken: the client authenticates itself.

**Delegated tokens can do only what both may.** Each request with one must
be allowed by the person's roles and by the service account's, within the
delegation's scopes and projects. A delegation can't take the person
anywhere the service account can't go, or the other way round. It stops
working when the delegation expires or is revoked, or when either the
person or the service account is deactivated.

**It's recorded as the person's, through the agent.** Changes name the
person, and the credential as the agent, with the delegation as its grant
(ADR 0072). `/me` shows whom a token acts for, and which agent acts.

**Approvals stay with people.** A delegated token is a token, so it can't
approve an agent's runs (ADR 0050).

## Consequences

- Positive: unattended work has a person responsible for it, chosen by
  them, bounded by what they allowed and by when.
- Positive: neither side can widen the other. A powerful service account
  doesn't make a person more powerful, nor the reverse.
- Negative: a delegation lasts until it expires, so a person who leaves
  and is deactivated stops theirs at once, but one who only changes teams
  keeps it, within their new roles.
- Negative: a credential allowed to act for people is a higher-value
  secret. Org Admins should allow it only for credentials kept in a secrets
  manager.
