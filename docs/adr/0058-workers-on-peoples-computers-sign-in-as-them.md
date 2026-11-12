# 0058. A worker on someone's own computer signs in as them

- Status: Accepted
- Date: 2026-11-12
- Refines: [0043](0043-workers-lease-jobs.md),
  [0028](0028-oauth-authorization-server.md)

## Context

The client tier runs actions on the launcher's own computer: their GPU,
their licensed desktop software, the files on their disk. A deployment's
workers register with a registration token a Compute admin makes
([ADR 0043](0043-workers-lease-jobs.md)), which suits pods and VMs. On
someone's laptop, that would mean asking an admin for a secret, pasting it
into an environment variable and keeping it there. The worker would also
belong to no one, so it couldn't tell whose runs it should take.

Pharma IT expects a desktop agent to sign in the way their other tools do:
in the browser, through the company's SSO, with a record of who allowed
what. GitHub's and GitLab's CLIs do this with OAuth's device flow, which
Iki's CLI and SDK already use ([ADR 0051](0051-the-python-sdk.md)).

## Decision

**`iki-worker login <server>` signs in with the device flow.** It asks for
a code as the `iki-worker` client, opens the person's browser (or prints
the page and code), and polls until they confirm. On the consent page they
choose the projects it may run their actions in. `iki-worker` is a public,
first-party client that the server creates in a migration. It uses only the
device grant, and may ask only for the **`worker` scope**. No other client
or service account credential may ask for that scope, and a `worker` token
can't call Iki's API or MCP.

**Registering spends the access token.** The worker sends the token to
`/worker/v1/register` once. The server checks the token is current, comes
from `iki-worker`, and is for an active person who still allows the app.
It then creates a client-tier worker owned by them, revokes the token and
its refresh token, and audits `worker.registered`. The worker keeps only
its own `iki_wkr_` token, in `~/.config/iki/worker.json` (mode 0600)
unless `IKI_WORKER_STATE` says otherwise. `iki-worker start` then needs no
`IKI_URL` or registration token.

**It runs only its person's runs, in the projects they allowed.** A
client-tier job is owned by whoever launched it, and a client-tier worker
leases only its owner's jobs. If the person narrowed the app to some
projects, it leases only jobs in those. If they revoke the app, it leases
nothing.

**People manage their own workers.** _Your workers_, under the account
tabs, lists their signed-in workers, shows whether each is online and what
it's running, and revokes them. Revoking the `iki-worker` app under
_Authorized apps_ revokes all of them. A deactivated person's workers stop
working at once. `iki-worker logout` deregisters the worker and deletes
its token.

**A run says when it's waiting for one.** A queued client-tier run whose
requester has no worker online says so. If you're the requester, it says
how to start or sign in a worker.

## Consequences

- Signing a worker in goes through the deployment's SSO and MFA, and is
  recorded as a consent, a device approval and a worker registration.
- A leaked state file gives a client-tier worker's token, which can lease
  only its owner's client-tier runs. It's revoked from _Your workers_, by
  revoking the app, or by deactivating the person.
- Signing in again on the same computer replaces the earlier worker.
- Compute admins still see and revoke every worker, client tier included.

## Alternatives considered

- **Registration tokens for laptops.** These need an admin for every
  laptop, give an ownerless secret, and say nothing about whose runs the
  worker should take.
- **Keeping the OAuth token and refreshing it.** The worker would hold a
  credential for more than registering. A refresh token can be replayed,
  and revocation would depend on the worker asking. A worker's own token is
  narrower, and the server checks it on every call.
- **A loopback redirect (authorization code with PKCE).** That needs a
  browser on the same machine. The device flow also works over SSH and on
  a headless workstation.
