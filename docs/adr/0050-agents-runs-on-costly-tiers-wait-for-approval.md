# 0050. An agent's runs on costly tiers wait for the person's approval

- Status: Accepted
- Date: 2026-11-11

## Context

ADR 0013 lets agents run cheap actions on their own, and holds runs on the
container and VM tiers for a person's approval. This record says how the
hold works. A runaway or misled agent on those tiers could spend a lot of
money, and an agent must never be able to approve its own request. Regulated
labs also expect who allowed costly work, and when, to be on the record.

## Decision

- **The rule is the tier.** A run launched through MCP on the container or
  VM tier waits for approval. Runs on the client and backend tiers, and every
  run a person starts themselves, queue at once. Iki can't estimate what a
  run will cost before it runs: its duration isn't known, and the price is
  a property of the machines on those tiers. So "above the user's cost
  threshold" (ADR 0013) is these tiers.
- **One request per launch.** A launch's runs share one `RunApproval`. A
  batch is approved or declined together, so the person makes one decision.
  The request records who it acts for and the OAuth app that asked.
- **Held, not queued.** The runs are `awaiting_approval`, and their jobs are
  pinned to the plugin code they were launched with (ADR 0047), in a state
  no worker leases. Waiting runs don't count against waiting-runs quotas.
- **Only the person decides, only in the web app.** The person the app acted
  for approves or declines on the run's page. They're told by a notification
  that links to it. Approving or declining is refused to every OAuth token
  (`web_only`), whatever its scopes, so no agent, including one with the
  `run` scope, can approve its own request.
- **Approving checks again.** The person must still be allowed to run the
  tier in the project, the project must not be archived, and the
  waiting-runs quotas must have room. Then the jobs join the queue as if just
  launched.
- **Declining ends the runs** unstarted, with an optional note the agent can
  read. Cancelling every run a request holds settles it too.
- **Requests expire after 7 days.** The `maintain` job expires requests
  nobody decided, ending their runs. A request that's due is treated as
  expired at once, even before the job runs.
- Each step is audited: `run.approval_requested`, `run.approved`,
  `run.declined` and `run.approval_expired`.
- The MCP tools `run_action` and `run_batch` tell the agent, outside the
  data fence, that its runs wait, and give the page where the person
  decides.

## Consequences

- Positive: costly work started by an agent always has a person's decision
  on the record, made where an agent can't act.
- Positive: approving later still runs exactly the code that was asked for.
- Negative: an agent's container and VM runs wait on a person, by design.
  Agents can tell the person where to look, but can't speed it up.
- Negative: a person can't approve from the command line or the API. That's
  deliberate: those use tokens, which agents also hold.

## Alternatives considered

- **Approving with a scope:** a scope an agent could be granted would let it
  approve its own runs.
- **A per-person cost threshold:** needs a cost estimate Iki can't make
  before a run, and the tiers already separate cheap from costly.
- **One request per run:** a batch of a hundred would need a hundred
  decisions.
