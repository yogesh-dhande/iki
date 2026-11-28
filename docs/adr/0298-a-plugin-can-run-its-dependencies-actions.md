# 0298. A plugin can run its dependencies' actions

- Status: Accepted
- Date: 2026-11-27
- Amends: [0079](0079-workflows-are-dags-of-ordinary-runs.md) (a step runs
  its own plugin's actions), [0080](0080-triggers-run-actions-when-entities-change.md)
  (a trigger runs one of the plugin's actions),
  [0170](0170-a-component-can-offer-its-plugins-actions-on-its-entity.md)
  (a component offers its own plugin's actions)
- Updates: [0222](0222-facilities-choose-automatic-calibration-tracing.md)
  (the calibration tracing example runs Equipment's own trace)

## Context

A lab that subtypes a first-party type wants that type's calculations on its
records. `acme/nanobodies.Nanobody` extends `iki/protein.Protein`, and the lab
wants each new nanobody's molecular weight and isoelectric point, which
`iki/protein.compute_properties` already computes. Until now a trigger, a
workflow step and a component's run button could name only their own
plugin's actions. ADR 0079 said steps from other plugins would come later. So
the lab had to copy the action manifest, its environment, its lockfile and
its script into its own plugin.

Two examples in this repository were such copies. The translation workflow
example declared local copies of Protein's `translate` and
`compute_properties`. The calibration tracing example declared a local copy
of Equipment's `trace_failed_calibrations` for its trigger. A copy drifts from
its source when the source is fixed. In a regulated lab it is also a second
calculation to validate. The lab wants to run the one the vendor validated,
on its own records.

GitHub Actions has the same pattern. A workflow `uses:` an action from another
repository, and gives it only inputs. The action runs as its author declared
it. The calling workflow can't change what the action's code does or what it
may reach. Slack's workflow builder works the same way: an app's steps can be
used in anyone's workflow, and each step keeps the app's own permissions.

## Decision

**A qualified name runs a dependency's action.** A trigger's `action:`, a
workflow step's `action:` and each name under a component's
`events.run.actions` may be `namespace/plugin.action`. A bare name is still
the plugin's own action. This is the same rule as for type references.

- The plugin must list the other plugin under `dependencies`. `{}` is enough
  when it uses none of that plugin's types.
- The build reads the dependency as it reads its types, from `--with` or the
  deployment's lockfile.
- An undeclared plugin is refused (`undeclared-dependency`). So is a name
  without its namespace (`unqualified-reference`) and an action the
  dependency doesn't have (`trigger-unknown-action`, `workflow-unknown-action`,
  `component-unknown-action`, each naming the dependency and suggesting the
  nearest name).
- The wiring is checked exactly as for a local action:
  - each input that takes an entity takes the trigger's type or the step's
    source, subtypes included;
  - every required input and param is given;
  - each param value fits;
  - `related:` inputs take many;
  - a condition reads a metric the action declares;
  - `$steps.<id>.created.<Type>` names a type the action creates;
  - a component's tab type is one of the action's input types.

**The run is the action's, launched by the caller.** The action runs exactly as
its own plugin declares it:

- its environment, image and tier;
- its plugin's version and commit;
- its plugin's deployment settings, and the project's starter for that plugin;
- its secrets, network hosts and outside APIs;
- its resources, timeout and retries;
- its effects.

Nothing of the calling plugin's goes into the run. As before, the run belongs
to whoever launched it, in the project it was launched in:

- a trigger's run belongs to the Admin who turned the trigger on, or to the
  service account they chose;
- a workflow step's run belongs to the person who started the workflow;
- a component's button opens the run page, and the run belongs to the person
  who starts it there.

The project's budget, quotas, approvals and result reuse apply as they do to
any run. Provenance keeps both sides:

- the run records the action by its qualified name, with that plugin's
  version and commit;
- a trigger firing records the trigger by its qualified name;
- a workflow run records its workflow, with that plugin's version.

The run page names the trigger or workflow, and its plugin when that plugin
isn't the action's. The Triggers and Workflows pages say which plugin's
action each trigger or step runs. An Admin turning a trigger on sees that the
run will come from another plugin.

**Calling an action never widens it.** A trigger or step decides only when the
action runs, on which entities and with which values of its declared params.
Nothing it says can make the run do more than the action declares:

- **Writes.** The server accepts a run's output only within the action's own
  declared `creates` and `updates`. The entities it updates must be ones the
  launcher may edit.
- **Params.** Values are checked against the action's own params, at build
  time and again at launch.
- **Secrets, hosts and APIs.** These come from the action's manifest only.
  Each outside API must be approved in the project, and the launcher
  consents to what is sent (ADR 0130). A workflow's consent covers its steps.
- **Components.** A component still can't start a run. The most it can do is
  open the run page with an action it declared already chosen.

Everything a cross-plugin trigger, step or button starts is a run that someone
with that role could start by hand from Run action in that project. What
automation adds is when it starts. Triggers stay off in every project until
one of its Admins turns them on (ADR 0080). Plugins aren't enabled
project by project, so that opt-in is the project's control, and it now names
the action's plugin.

**A recheck runs only its own plugin's action.** A recheck trigger (ADR 0080's
amendment) is on by default in every project. In the edit's own transaction
it clears the computed values it rechecks. Only the plugin that declares a
calculation may make it run on every edit without an Admin turning it on. So
a trigger with `recheck:` must run its own plugin's action
(`trigger-recheck`). Another plugin can still declare an ordinary trigger
that runs the same calculation, and a project's Admin turns it on.

**A detached dependency stops its callers' runs, without turning anything
off.**

- A trigger whose action's plugin is detached (ADR 0091) skips its firings and
  stays on, as it does when its own plugin is detached.
- A workflow with such a step says it can't run here.
- A component no longer offers the action.

**Builds catch a dependency's change.** Actions have no schema version of their
own. `iki deploy build` checks every plugin against the others in the
lockfile. A release of the dependency that renames, removes or narrows an
action therefore fails the calling plugin's build, before any run fails.
Iki already refuses to purge a plugin that another depends on.

## Consequences

- A lab gets one validated calculation, run on its own subtypes. It doesn't
  maintain a copy. The translation workflow example now runs Protein's two
  actions. The calibration tracing example's trigger runs Equipment's own
  trace. Both examples dropped their copied actions, environments, scripts
  and lockfiles.
- When the dependency fixes the calculation, the calling plugin's runs get the
  fix at the next deployment. The lab reviews the dependency's release notes,
  as it would for its types, and runs its own tests on that build.
- An action's author can't keep it from being called. Any person with the role
  can already launch any installed action, so a trigger or step adds no reach.
- Action tests (`tests/*.yaml`) still test only the plugin's own actions. A
  lab tests its trigger or workflow by building it with the dependency.
- Workflows that a project defines, rather than a plugin
  (`core.WorkflowDefinition`), still come later.
