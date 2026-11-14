# 0080. Triggers run actions when entities change

- Status: Accepted
- Date: 2026-11-13
- Refines: [0013](0013-agents-via-mcp-oauth-approvals.md),
  [0079](0079-workflows-are-dags-of-ordinary-runs.md)

## Context

Some work should just happen. When a protein is added, its molecular
weight and isoelectric point should be computed. When a sample is
registered, its storage should be assigned. Today someone has to remember
to launch the run. Labs want such runs to start by themselves. They also
want each one to stay a person's run, recorded as started automatically,
and never able to loop.

GitHub Actions runs workflows on repository events. A repository's admins
choose whether Actions may run. Events caused by a workflow's own token
start no new workflows, which is how GitHub stops loops. Slack's workflow
triggers are added by people the workspace admins allow, and run as them.

## Decision

**A plugin declares triggers next to its actions.** Triggers live in
`triggers/*.yaml`, with `kind: Trigger`. They are named in snake_case and
qualified by the plugin (`protein.compute_new_protein_properties`).

- `on` is `entity.created` or `entity.updated`.
- `type` is an entity type; subtypes count too.
- `run` is one of the plugin's actions.
- `input` is the action's input that takes the entity. It can be left out
  when only one input can take it.
- `params` are fixed values.
- `fields` narrows `entity.updated` to changes of those fields, as
  GitHub's `paths` filter does.

The build checks that:

- the type exists and has the fields;
- the action takes an entity of the type in one input and needs no other;
- the params fit;
- the action doesn't run on the client tier, where nobody may be at their
  computer.

**Installing a plugin turns nothing on.** Each trigger is off in every
project until one of the project's Admins turns it on there
(`project.triggers.manage`). They can do that only in the web app; tokens
can't, so no agent can set up standing work in someone's name. Turning a
trigger on, or off, is audited.

**Its runs are the Admin's.** A trigger runs as whoever turned it on last,
and it is refused to an Admin who can't run its action's tier in the
project. Each run it starts:

- is an ordinary run, launched as that Admin with `via=trigger` and a
  reason naming the trigger and the change;
- is checked as any run they launched would be: their role, the project's
  budget, quotas, reuse of identical results (ADR 0057);
- waits for that Admin's approval on the container and VM tiers, as an
  agent's runs do (ADR 0013).

Approval requests name the trigger that asked. If the Admin's role no
longer lets the run start, Iki turns the trigger off, records why, and
tells the project's Admins. It never runs as anyone else.

**Firings are recorded with the change.** A change only fires triggers in
the entity's owner project. In the same transaction as the change, each
enabled trigger it fires gets a `TriggerFiring`: the entity, the revision
the change made, and the event. A task launches the run once the change has
committed. Task runners pick up any firing whose task was lost, each
minute. A firing ends in one of three states:

- `launched`, with its run;
- `failed`, saying why;
- `skipped`, when the trigger was turned off or the entity archived before
  it could fire.

Each project lists its triggers and what they fired. A run's page names the
trigger and the change that started it.

**What a triggered run writes fires nothing.** This is GitHub's rule, and
it is the whole of loop protection. A trigger can't fire itself, or set
off another that fires it back, however triggers are combined. Chaining
actions belongs in a workflow (ADR 0079), where the chain is declared and
reviewed. Changes made by people, their own runs, workflow steps and
agents fire triggers as usual. A bulk import's entities (ADR 0056) fire
nothing one by one: a trigger can't start a run for each of millions of
rows.

## Consequences

- Automatic work stays accountable. Every triggered run names a person, the
  trigger and the change. Its cost counts against the project's budget,
  and costly tiers still need a person's yes.
- Loops are impossible by construction, with no depth limit to tune. The
  price is that a trigger can't build on another trigger's output. That
  needs a workflow.
- A trigger whose Admin leaves stops, and says so, rather than quietly
  running under someone else.
- Scheduled triggers (cron, run as a service account, never overlapping)
  build on the same records. They come next.
