# 0070. Runs show what they cost

- Status: Accepted
- Date: 2026-11-12
- Refines: [0041](0041-compute-templates-and-bindings.md),
  [0045](0045-run-quotas.md),
  [0061](0061-container-tier-runs-get-pods-of-their-own.md),
  [0062](0062-warm-pods-run-one-job-after-another-and-start-each-clean.md)

## Context

Container and VM runs use machines Iki starts. A GPU machine can cost tens
of dollars an hour, and labs pay for them out of project and team budgets.
A scientist deciding whether to screen a thousand proteins needs to know
what one run costs. A Compute admin needs to know which projects spent
what, and what was spent on machines that sat idle. Finance needs records
it can reconcile with the cloud bill. Cloud cost tools (AWS Cost Explorer,
Kubecost, OpenCost) put costs down to projects by their resources' labels.
GitHub Actions and Databricks show each job's or run's cost next to it.

## Decision

**A template has a price.** `price: { per_hour, currency }` is what one of
its machines costs an hour, whether it's running a job or not. All of a
deployment's prices are in one currency (`mixed-currencies`). The build
warns about a container or VM template without one (`unpriced-template`):
its runs show machine time but no cost. Backend and client tiers have no
per-run cost: their machines run anyway.

**Cost records say whose machine time it was.** Each record keeps the
launch, its template and tier, the period, the seconds, and the price and
amount it was recorded at. A run's record also keeps the run, project, team
and action, as they were.

- An **ephemeral** machine runs one job. All its life, from being asked
  for until it ends, is that run's, recorded when it ends. Starting and
  pulling the image are part of what a run costs.
- A **persistent** machine runs one job after another. The time from its
  worker taking an attempt to letting go of it is that run's, recorded
  then. The rest of the machine's life is `idle`, recorded when it ends.
- **Idle** time is the organization's, not any project's. A machine that
  never ran a job is idle all its life.
- A job that ran several runs of a batch at once (ADR 0055) is split
  evenly between them.

A machine's cost counts when it's recorded, so a report of a month holds
what was recorded in it.

**A run shows what it cost.** `cost` on a run is what's recorded, plus,
while a machine is still the run's, what it has cost so far
(`final: false`). The run's page says "$0.42 so far for 21 min of machine
time", or just machine time when the template has no price.

**An action says what a run may cost** before it's launched. Its `estimate`
names the template it would run on and its price. It also gives the median
total of its last 50 succeeded runs (`typical`, null until there are any),
and what a run that runs for its whole timeout would cost. The launch form
shows these, and multiplies them for a batch. A median ignores the odd
very long run, and the most a run can cost is always shown beside it.

**Reports are for those who answer for costs.** `costs.read`, given to
Compute Admins, Auditors and Org Admins, opens:

- `GET /compute/costs`: totals for a period, by project, team, template,
  action or tier. Idle machines are a group of their own. The web app's
  **Costs** page shows a month at a time.
- `GET /compute/costs/records.csv`: every record in the period, for
  finance. It isn't cached, and names that start like a spreadsheet
  formula are quoted so they can't run as one.

Anyone who can read a project sees `GET /projects/{id}/costs`, by action or
template, and a card on its overview when its runs cost anything. Periods
are days in UTC, this month unless given, and at most a year.

**Run pods are labelled with whose they are.** While a pod is a run's, the
provisioner labels it `iki.ikisci.org/run`, `/project` and `/team`, and
takes the labels off when a persistent pod is idle again. Kubernetes cost
tools can then put the cluster's own costs down to projects, as Iki's
records do. The provisioner may patch pods for this, and nothing else
about them.

**A run whose launcher can no longer run on its tier doesn't start.**
Whether the person who launched it still has the role the tier needs
(Runner for container and VM) is checked again when a worker leases it. A
queued run whose launcher was removed from the project, or lost the role,
is rejected (`not_allowed`) with a message saying so, and isn't retried.
Spending stays with people who may still spend.

## Consequences

- Positive: scientists see what a run costs before and after it runs.
  Compute admins see where money goes, idle machines included, and finance
  gets records to reconcile with the bill.
- Positive: records keep the price, project and team as they were, so
  changing a price or moving a project doesn't rewrite what was spent.
- Positive: Iki's records and the cluster's own cost tools agree on whose
  each pod was.
- Negative: prices are the deployment's own figures, not the cloud's bill.
  Spot prices, discounts and storage aren't in them. Reconciling them is
  finance's job.
- Negative: a long-lived persistent machine's idle time is recorded only
  when it ends, so a month's idle cost can show up in the next month.
- Budgets that warn and stop spending build on these records
  ([ADR 0071](0071-budgets-warn-and-can-stop-spending.md)).
