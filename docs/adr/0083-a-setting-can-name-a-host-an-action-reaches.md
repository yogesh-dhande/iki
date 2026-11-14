# 0083. A setting can name a host an action reaches

- Status: Accepted
- Date: 2026-11-13
- Refines: [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0053](0053-runs-reach-the-internet-through-an-egress-proxy.md)

## Context

An action names the hosts its runs may reach under `network` (ADR 0053), and
the egress proxy refuses every other host. The names are fixed when the
plugin is written. That works for public services, such as NCBI, but not for
a LIMS or an ELN: each lab runs its own, at its own address, and a plugin
that connects one is written once for every deployment.

Allowing any host would undo what `network` is for. Asking each deployment
to fork the plugin and edit its hosts would leave labs running code nobody
reviewed.

## Decision

**A `network` entry can be `{settings.<name>}`**, where the setting is a
string with the new `format: hostname`: a DNS name in lower case, without a
scheme or port, since runs reach hosts over HTTPS on port 443 only. The
plugin's checks refuse an entry naming a setting it doesn't declare
(`network-unknown-setting`) or one that isn't a hostname
(`network-setting-not-hostname`). An entry is a whole host: a setting can't
be part of one, so it can't widen a wildcard.

**A deployment's build fills the host in.** Settings are already checked
against their format and written into the build (ADR 0034). The build now
also writes each action's hosts, with the deployment's values, into the
manifest the server reads, so the egress proxy, the run's page and the
catalog all see the real host. A setting left unset reaches nothing: its
entry is dropped. Changing the host means changing the config repository and
rebuilding, which is reviewed like any other change to a deployment.

## Consequences

- One plugin connects each lab's LIMS; the deployment's config repository
  says which one, next to its other settings.
- What a run may reach is still decided before it runs, and shows on its
  page as before.
- `hostname` is also a format any string field can use.
