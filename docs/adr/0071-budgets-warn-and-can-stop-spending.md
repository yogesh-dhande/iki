# 0071. Budgets warn, and can stop spending

- Status: Accepted
- Date: 2026-11-12
- Refines: [0045](0045-run-quotas.md),
  [0050](0050-agents-runs-on-costly-tiers-wait-for-approval.md),
  [0070](0070-runs-show-what-they-cost.md)

## Context

Labs pay for GPU machines from project, team and organization budgets. A
batch of a thousand structure predictions, or an agent in a loop, can spend
a month's money in a day. Seeing costs afterwards (ADR 0070) isn't enough.
Compute admins need to hear before a budget runs out, and some need
spending to stop when it does. Cloud precedent is AWS Budgets and GCP budget
alerts: thresholds that notify, and actions that stop spending. GitHub
Actions' spending limits do the same: jobs wait once the limit is reached.
Quotas (ADR 0045) count jobs, not money.

## Decision

**A budget** covers the organization, a team or a project, at most one per
scope. It's a monthly amount in the currency of the deployment's prices.
Months are calendar months in UTC. Only Compute Admins set, change and
remove budgets, under `/api/v1/compute/budgets`, and every change is
audited. Those who can read costs (Compute Admins, Auditors, Org Admins)
see them all. A project's readers see the project's own budget on its costs
card.

**Spent** is what's recorded this month in the budget's scope (ADR 0070),
plus what machines still working for its runs have cost so far. A team's
budget counts what its projects spent while they were in it, as the records
say. The organization's budget counts idle machines too, once their time is
recorded.

**Warnings.** A budget's `warn_at` percentages (50, 80 and 100 unless set)
are each told once a month as spending passes them:

- Compute Admins are notified, and so are a project's Admins for the
  project's own budget. This uses the new notification kind
  `budget.threshold_reached`, which people can also choose to get by email.
- It's audited, and sent to webhooks as `budget.threshold_reached`: to the
  organization's, and to the project's for a project's budget.
- Passing several thresholds at once is one message, about the highest.

Task runners check every minute. A threshold is recorded once, whichever
runner gets there first, so a message is never sent twice.

**Enforced budgets stop spending.** With `enforce`, once a budget is spent:

- New runs on the container and VM tiers in its scope are refused at
  launch, with a message naming the budget, what's been spent and how to go
  on.
- Queued runs there wait. They aren't leased, and the provisioner starts no
  machine for them. The run says why ("The project's budget for November is
  spent ($512.30 of $500.00)."). They start when the month ends, or when the
  budget is raised or removed.
- Runs already going finish. Stopping them would waste what they've cost
  so far.
- A launch whose runs typically cost more than what's left (the action's
  estimate times the number of runs) is refused before it starts.
- Approving an agent's request (ADR 0050) checks again, since spending may
  have gone on in the meantime.

Backend and client tiers are never held: their machines run anyway.

## Consequences

- Positive: those who answer for money hear before it's gone, and can make
  sure it can't be overspent by more than what's already running.
- Positive: a refused launch says exactly why and what to do, and a waiting
  run says what it's waiting for.
- Negative: spending can pass an enforced budget by what was running when it
  ran out, and by idle time recorded late. It's a limit on starting work,
  not a hard cap on the bill.
- Negative: checking spending on each lease costs a query per enforced
  budget. Deployments have a handful of budgets, so this stays cheap.
- Budgets are monthly only. Quarterly or yearly budgets can come later as
  another period on the same records.
