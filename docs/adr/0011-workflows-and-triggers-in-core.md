# 0011. Workflows and triggers in core, on Iki's own DAG engine

- Status: Accepted
- Date: 2026-11-09

## Context

Real scientific pipelines chain actions: for example MSA, then AlphaFold2,
then scoring. Teams also want routine work to happen automatically, such as
computing properties whenever a new protein is created. Each step needs the
same provenance, permissions, cost tracking and reuse as a single run.
Iki already runs everything through its Postgres job queue (ADR 0005).

## Decision

Workflows and triggers are part of core and ship in M1.

- **Workflows** are `kind: Workflow` YAML describing a DAG (directed
  acyclic graph: steps with dependencies and no cycles) of actions. They
  support typed wiring between steps, fan-out over collections, fan-in,
  conditions in Iki's safe expression language, and approval gates.
- A workflow is defined by a plugin, or by a project as a
  `core.WorkflowDefinition` entity.
- Each step is a normal `ActionRun`. Iki's own scheduler advances the DAG
  on the job queue as steps finish. Workflows are resumable because
  completed steps are reused.
- **Triggers** are declarative, for example `{on: entity.created, type:
protein.Protein, run: protein.compute_properties}`. They are enabled per
  project. Triggered runs are recorded with `via=trigger`.
- **Loop protection** stops a run from triggering on its own outputs.
  Triggers respect cost limits and approval thresholds.

## Consequences

- Positive: lineage, provenance, permissions, reuse and cost accounting are
  the same for workflow steps as for single runs.
- Positive: no separate orchestration service to deploy and secure.
- Positive: approval gates use the same approval flow as agents
  (ADR 0013).
- Negative: we build and maintain a DAG scheduler ourselves.
- Negative: loop protection and cost limits must be precise, or triggers
  can run away and spend money.
