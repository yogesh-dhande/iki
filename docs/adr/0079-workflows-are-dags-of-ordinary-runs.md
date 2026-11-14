# 0079. Workflows are DAGs of ordinary runs

- Status: Accepted
- Date: 2026-11-13

## Context

Scientists chain actions: translate a DNA sequence, then characterize the
protein it codes for; assign a sample's storage, then decode it. Today each
step is launched by hand once the last one has finished, and nothing records
that the runs belonged together. A workflow describes such a chain once, so
it runs the same way every time and its runs can be followed as one.

## Decision

**A plugin declares workflows next to its actions.** `workflows/*.yaml`,
`kind: Workflow`, named in snake_case and qualified by the plugin
(`protein.translate_and_characterize`), like an action. It has entity
`inputs` and `params` as an action does, and `steps`, each running one of
the plugin's actions, by name. Its build checks the workflow against them:

```yaml
apiVersion: iki/v1
kind: Workflow
name: translate_and_characterize
label: Translate and characterize
inputs:
  sequence: { type: dna.DNASequence }
params:
  frame: { type: int, min: 1, max: 3, default: 1 }
steps:
  translate:
    action: translate
    inputs: { sequence: $inputs.sequence }
    params: { frame: $params.frame }
  properties:
    action: compute_properties
    inputs: { protein: $steps.translate.created.Protein }
```

**Steps are wired by reference, and the build checks the types.** A step's
input takes the workflow's input (`$inputs.<name>`), the entities a step
created of a type (`$steps.<id>.created.<Type>`), or what a step was given
(`$steps.<id>.inputs.<name>`), after it has run, so a step that updates an
entity comes before one that reads it. A param takes a value, or the
workflow's param (`$params.<name>`). A string that starts with `$` is
written `$$`. The plugin build refuses a reference to nothing, an entity
of the wrong type, many entities where a step takes one, a param of the
wrong type, and a step that comes before itself. Steps run in the order
their references (and any `needs: [<id>]`) give: those that don't depend on
each other run side by side.

**A step can run once for each of many entities.** `each: <input>` names
one of its action's inputs that takes one entity: the step is launched as a
batch (ADR 0055), one run for each entity its source gives, up to 1,000, and
succeeds once every run has. A run that fails doesn't stop its siblings:
their results are as valid as if each had been launched alone. What the step
created, and what it was given for that input, are then many, so a later step
takes them all where its input takes many (fan-in), or runs for each in turn.
The build allows many entities into an input that takes one only there.

**A step can run only if conditions hold.** An action declares the numbers
its runs report about themselves (`metrics`: an alignment's identity, a
structure's confidence). A step's `when:` lists conditions that must all hold
for it to run, each comparing the workflow's param or an earlier step's
metric with a value or param, by `is`, `is_not`, `in`, `at_least`, `at_most`,
`above` or `below`. They're data, not expressions: the build checks each
names a metric the step's action declares, from a step that runs once, and
compares it with something of its type, so nothing is evaluated that a
reviewer can't read at a glance. A step whose conditions don't hold is
skipped with the reason, and so are the steps after it, as in GitHub Actions;
the workflow succeeds, since it did what it says. A metric its run didn't
report fails the step rather than count as false: whether it should have run
isn't known, and in a regulated lab a silent skip is worse than a stop. A
reused run's metrics are those of the run it reused.

**A step can wait for a person to approve it.** `approval: { role,
instructions }` holds a step, once its turn comes and its conditions hold,
until someone with that project role (Editor by default) approves it, as
GitHub's environments wait for required reviewers. Whoever started the
workflow can't approve its steps: a second person checks, as GxP review
expects, and as setup builds already require (ADR 0075). Deciding is done in
the web app only, so no agent, whatever its token's scopes, approves a step
(as ADR 0050). Approving, with an optional note, launches the step as the
workflow's starter, under their authority, checked again then; the approver
is recorded, not made the launcher. Declining needs a reason, fails the
workflow, and tells its starter. A step nobody decides within 30 days (as
GitHub's limit) is declined. Those who may decide are notified, and each
request, decision and expiry is audited with the note. A Part 11 signature,
signing again at the moment of deciding, can be added once Iki signs people
in other than through SSO (ADR 0023).

**Each step is an ordinary run, launched as the person who started the
workflow.** It's launched as if they had launched it themselves, in the
workflow's project, with the same checks (their role on the tier, the
project's budget and quotas, approval if an agent asked), and records the
workflow and its step. A deterministic step whose identical run already
succeeded reuses its results (ADR 0057), so starting a workflow again
reuses what's done. A step that can't be launched, or whose run doesn't
succeed, fails the workflow: no further step starts, those running finish,
and the workflow then ends failed. Cancelling a workflow cancels its running
steps' runs.

**Steps run as the workflow was started.** A later step is launched by the
server, not in the starter's request, so it's launched in the audit context
the workflow was started in: through the web app, the API, or an agent over
MCP (whose runs on the container and vm tiers then wait for approval, as the
agent's own would). The starter's role is checked again at each launch: one
who lost it fails the step rather than run on their old authority.

**A workflow run is followed as one.** It has a HRID (`WF-000001`), its
inputs at the revisions they were given, its params, the workflow as
installed at the start, each step's state (waiting, running, succeeded,
failed, cancelled, or skipped once nothing more will start) and run, and who
started it and how. People who can read the project read it
(`GET /workflow-runs/{ref}`, its runs with `GET /runs?workflow=`), and its
page follows it live. Starting and cancelling one are audited. A run that
ends queues a task, in the transaction that ended it, that moves its
workflow on; the task runners also look every minute for any left behind.

## Consequences

- A workflow is as reproducible as its runs: each records its inputs at
  the revisions it read, and the workflow records which run was which step.
- The web app's graph and launch form, and re-running from a failed step,
  follow (step 38e), as do MCP tools for agents to start and follow
  workflows.
- Conditions read params and metrics only, not entities' values: a step that
  should run only for entities with some value takes the entities a query
  step gives it.
- A step runs its own plugin's actions: a plugin's build sees only its own,
  and other plugins' types. Steps from other plugins, and workflows defined
  by a project rather than a plugin (`core.WorkflowDefinition`), come later.
