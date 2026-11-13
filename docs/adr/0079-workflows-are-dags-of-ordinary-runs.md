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
- Fan-out over many entities, conditions, approval gates, and the web app's
  graph and launch form follow (steps 38b to 38e), as do MCP tools for
  agents to start and follow workflows.
- A step runs its own plugin's actions: a plugin's build sees only its own,
  and other plugins' types. Steps from other plugins, and workflows defined
  by a project rather than a plugin (`core.WorkflowDefinition`), come later.
