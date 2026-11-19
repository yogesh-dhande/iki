# 0141. Setup builds don't call outside APIs

- Status: Accepted
- Date: 2026-11-18
- Refines: [0075](0075-environment-setup-builds-and-managed-volumes.md),
  [0130](0130-outside-apis-are-approved-per-project-and-called-through-the-proxy.md)

## Context

An environment's setup build fills its volumes once, for every project's runs
(ADR 0075). It runs on a machine of its own and reaches the hosts its setup
lists under `network`, through the egress proxy. It belongs to the
deployment: a Compute Admin asks for it, with a reason, another may have to
approve it, and its cost is the deployment's.

Runs call outside APIs, models behind a company's service, under the rules
of ADR 0130: an Org Admin approves each API for each project; the person
launching consents to what's sent; the egress proxy adds the organization's
key, records each call, and prices it, so it's a cost of the run's project
and counts against its budgets.

Could a setup build call one? The case for it is a build that fetches a
model's weights from a provider that serves them through an API, behind a key.
But every one of ADR 0130's rules is about a project:

- **Approval** is per project, because a project's data and purpose decide
  where it may go. A build has no project.
- **Consent** is the launching person agreeing to what of their data is sent.
  A build sends no lab data, and nobody launches it on their data.
- **Cost** is a cost of a run's project, team and action, and budgets are a
  project's or a team's. A build's would have no project to land on.

Making each deployment-level (an approval for "the deployment", consent by the
Compute Admin, a cost with no project) would make three exceptions to rules
that are worth having because they're uniform, and buy little: what a build
needs from outside is files, which is a download.

There was also a gap. A setup's `network` could name an API's host, which the
egress proxy would simply tunnel to: the build would reach the API without its
key, unrecorded, unpriced, unapproved, and nothing said so. And a setup's
`network` could name a host by a setting (`{settings.<name>}`), as an
action's can, but nothing replaced it with the host the deployment gives, so
the build could reach nothing there.

## Decision

**Setup builds don't call outside APIs.** They download what they need, such
as a model's weights or a database's release, from the hosts that serve the
files, listed under the setup's `network` (ADR 0053), with what they reached
recorded with the build and the data version they write as its provenance
(ADR 0075).

The refusal is early and says why:

- **`apis` under `setup`** is refused by the plugin's checks, with the reason
  rather than "isn't allowed here": "Setup builds can't call outside APIs: a
  build belongs to the deployment, so there's no project to approve an API
  for, nobody to consent to what's sent, and no project to bill."
- **A setup host that's one of the plugin's APIs'**, as written or by the same
  setting, is refused by `iki plugin validate` (`setup-calls-api`).
- **`iki deploy build`** resolves each setup's hosts from the deployment's
  settings, as it does actions', and refuses a setup whose host is any
  installed plugin's API's, naming both: "acme/prediction's alphafold-gpu
  setup downloads from fold.models.example, which is acme/protein.fold's host
  (FoldCloud, an outside API)." Only there are hosts that settings give, and
  other plugins' APIs, known. A deployment with such a setup doesn't build,
  so no egress proxy is ever given one.

A setup's `{settings.<name>}` hosts are now replaced by the deployment's, as
actions' are, and checked as actions' are: the setting must exist and hold a
host name.

## Consequences

- Positive: ADR 0130's rules hold everywhere an outside API is called: every
  call has a project that approved it, a person who consented, and a project
  that pays.
- Positive: a build can't reach an API's host around the proxy's key
  injection and records, whether its plugin's own or another's.
- Positive: setup hosts can come from settings, as actions' do.
- Negative: a provider that serves weights only through an authenticated API
  can't be a setup's source. Setups have no secrets today, so a download
  behind a key can't be one either; giving setups credentials of their own is
  a separate decision, about downloads, not outside APIs.

## Alternatives considered

- **Deployment-level outside API calls for builds:** an approval for the
  deployment, the requesting Compute Admin's consent, costs with no project.
  Three exceptions to ADR 0130's rules, for files that are better fetched as
  downloads.
- **Refusing only at the egress proxy, when the build runs:** the refusal
  would come hours after the plugin was installed, from a build a Compute
  Admin asked for, rather than to the plugin's author and the deployment's
  operator when they could fix it.
