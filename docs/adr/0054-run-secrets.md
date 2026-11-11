# 0054. Runs get the secrets their action declares, from the deployment's secret manager, one job at a time

- Status: Accepted
- Date: 2026-11-11
- Refines: [0040](0040-action-and-environment-manifests.md), [0046](0046-how-iki-worker-runs-a-command.md)

## Context

Some actions need credentials: an API key that raises NCBI's rate limit, a
token for the lab's LIMS or electronic lab notebook. Pharma companies keep
such credentials in a secret manager (HashiCorp Vault, AWS Secrets Manager,
Azure Key Vault) and expect them to be rotated there, never copied into
another system's database, and seen only by what needs them.

A run's command is plugin code that runs as the same user as its worker. A
worker pod that held every plugin's secrets would let any command read them
all. A plugin could also declare another plugin's secret by name and get it.

## Decision

**Actions declare secrets by name**, as now: `secrets: [LIMS_TOKEN]`. A
secret may be optional (`{name: NCBI_API_KEY, required: false}`), with a
`description` that tells administrators what it's for and where to get one.

**The deployment keeps them, per plugin.** Administrators keep run secrets
in their secret manager and sync them into one Kubernetes Secret (External
Secrets, or a CSI driver), with a key per secret named for its plugin:
`dna.NCBI_API_KEY`. A plugin gets only its own, even under the same name as
another's. Only web pods mount it, read-only, and they read it at each lease,
so a secret rotated in the secret manager reaches the next run without a
restart.

**Each job carries only its own**, as GitHub Actions gives each job its
secrets rather than storing them on runners. When a worker leases a job, the
server adds the secrets its action declares that the deployment has set.
The worker gives them to the command as environment variables. It leaves
them out of `input.json`, so a command can't copy them with its input by
mistake, and it puts `***` in their place in the log, including `::error`
and `::progress` lines. Each line of a multi-line secret is masked;
values under three characters aren't, as they would hide ordinary text.

**Only names are kept and shown.** Each attempt keeps which secrets its
worker was given, and the run's page lists them. An action's page and the
API say which of its secrets the deployment has set, and under which key,
never their values. A required secret that isn't set keeps the action from
running, and says which key an administrator should add.

**The dna plugin uses NCBI's API key** when it's set (`dna.NCBI_API_KEY`):
it sends it in the request body, not the URL, so it isn't in any access log,
and asks NCBI ten times a second instead of three.

## Consequences

- No secret is in Iki's database, its plugins' repositories or its logs.
  Rotation and access policy stay in the company's secret manager.
- A worker pod holds only the secrets of the job it's running, and only
  while it runs it. A command still sees its own secrets and could send
  them where its action reaches. Review the secrets and the hosts an action
  declares together.
- Masking is a safeguard, not a guarantee: a command that encodes a secret
  (base64, say) before printing it isn't masked.
- Client-tier actions can't declare secrets (Iki can't keep them from a
  person's own machine). Container and VM tiers will get theirs the same
  way, through their worker.
