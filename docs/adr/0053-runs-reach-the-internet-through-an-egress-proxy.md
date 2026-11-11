# 0053. Runs reach the internet through an egress proxy, and only the hosts their action declares

- Status: Accepted
- Date: 2026-11-11
- Refines: [0040](0040-action-and-environment-manifests.md), [0046](0046-how-iki-worker-runs-a-command.md)

## Context

Some actions must reach the internet. Fetching sequences from NCBI or
structures from the PDB is how much of a lab's data arrives. But a run's
command is plugin code that Iki didn't write, running next to other jobs'
data. Pharma security teams expect three things of anything that leaves
their network: it goes only where it was approved to go, a person can see
afterwards what went where, and nothing reaches inside the firewall that
shouldn't (server-side request forgery, the cloud metadata service). Many of
them also route all outbound traffic through a corporate proxy.

Actions already declare `network`, the hosts their runs may reach. Workers'
pods reach only the server and object storage (their NetworkPolicy). A
NetworkPolicy can't name hosts, only addresses, and the addresses behind
`eutils.ncbi.nlm.nih.gov` change.

## Decision

**An egress proxy is the only way out.** `iki-server egress_proxy` runs as
its own Deployment (`egressProxy` in the chart, off by default). Workers'
NetworkPolicy lets them reach it, the proxy accepts connections from
workers only, and it alone reaches the internet. Squid and the proxies
pharma IT run work the same way, and the clients commands use (`urllib`,
`requests`, `httpx`, `curl`) honour `HTTPS_PROXY` as they are.

**Each attempt has its own credentials.** When a worker leases a job for
an action that declares hosts, the server gives it the proxy's address with
the attempt's job ID and lease token as credentials. The worker hands the
command `HTTPS_PROXY` (and `HTTP_PROXY`, so plain-HTTP requests are refused
with a reason rather than time out). The credentials work while the attempt
holds its lease. When the attempt ends, or loses its lease, the proxy closes
its connections within 15 seconds. Runs of actions that declare nothing get
no proxy and reach nothing.

**The proxy lets a connection through only if:**

- it's a `CONNECT` to a host the action declares (`*.example.com` covers
  subdomains), on port 443. Plain HTTP is refused: everything is encrypted
  end to end, and the proxy never reads it;
- the host is named, not given as an address, and every address it resolves
  to is public, or in a network the operator allowed
  (`egressProxy.allowedNetworks`), as for webhooks. The proxy connects to the
  address it checked, so a rebinding answer can't reach inside the firewall;
- the TLS ClientHello names the same host. A tunnel to an allowed host can't
  be used to reach another site on the same address.

An attempt may hold 32 connections at once through each proxy, and an idle
one closes after five minutes.

**With a corporate proxy,** the egress proxy asks it to `CONNECT` instead of
connecting itself (`egressProxy.upstreamProxy`, with credentials from a
Secret). It still checks each host against the action, and the corporate
proxy applies its own rules and logs.

**What each attempt reached is kept.** For every host an attempt reached, or
was refused, Iki keeps the connections and bytes each way, or why it was
refused. The run's page shows the hosts the run may reach and, for each
attempt, what it reached and what it was refused. The API
(`GET /runs/{ref}/attempts`), MCP's `get_run` and the SDK show it too. The
proxy logs each connection as it opens and closes.

**Actions that declare hosts can't run without the proxy.** On a deployment
without one, they say so where people would launch them, and launching one
is refused.

**The dna plugin fetches from NCBI.** `dna.fetch_sequences` takes
accessions, asks NCBI's E-utilities for the GenBank records and imports them
as `import_sequences` does a file. It declares only
`eutils.ncbi.nlm.nih.gov`, follows NCBI's rules for automated clients (a
tool name, the lab's `ncbi_email`, three requests a second, backoff when told
to slow down), and records each sequence's NCBI record, version, link and
fetch time as its origin (ADR 0052).

## Consequences

- What leaves the network is what the deployment's plugins declare. An
  administrator reviews `network` when adding a plugin, as they review its
  code, and sees afterwards what each run reached.
- A command can't exfiltrate data to a host its action doesn't name, reach
  internal systems or the cloud metadata service, or keep a connection open
  past its attempt.
- Limits: a declared host could carry data elsewhere for a command that
  means to (an API that stores what it's sent). The proxy can't see inside
  TLS, so a site that serves several domains on one certificate could be
  reached under an allowed name. DNS lookups still leave from workers' pods.
  Declare as few hosts as an action needs.
- The proxy reads the database to check credentials and to keep what runs
  reached, so it holds database credentials like the web pods, and no
  others.
- Client-tier runs use the person's own network, as before: Iki can't limit
  a person's own machine. `iki-worker run --local` doesn't use the proxy.
