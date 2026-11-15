# 0093. Plugin action tests run fixtures through the worker

- Status: Accepted
- Date: 2026-11-15
- Refines: [0046](0046-how-iki-worker-runs-a-command.md)

## Context

A plugin's actions are code its authors change often: a new property, a
better parser, an upgraded library. A deployment runs them on its data,
and a lab relies on what they write. Authors need to know a change still
gives the results it gave, before a deployment pins it.

`iki-worker run --local` already runs one action from a hand-written
`input.json`. But a run input is long: the run, its plugin, the command,
the environment, IDs for every entity and file. Writing one per case is
tedious, and it drifts from the action's manifest. Nothing compared the
output with what it should be, either.

## Decision

**A plugin's tests are fixtures in its `tests/` directory.** Each YAML
document there of `kind: ActionTest` names an action and gives what a
person or run would give it:

- its `params`, and the plugin's `settings` if not their defaults;
- its `inputs`, each a list of entities by their values;
- the `files` those values and params name, as paths in the plugin.

`expect` says how it should end. Either it succeeds, and its output
contains `output`, or it fails with an `error` that contains the text
given.

**`iki plugin test` builds the run input the server would.** It takes
the command, environment and schema versions from the manifests. It gives
entities and files fixed IDs, and the run a fixed seed. It then runs the
action with `iki-worker run --local`, the same code as a leased job.
Then:

- it checks the output against `run-output/v1`, and against the action's
  declared effects, as the server would before writing it;
- it compares the output with `expect.output`. A mapping must have every
  key given, a list the same items in order, and a number equal to
  within `expect.tolerance` (relative; 1e-9 unless given). Anything left
  out isn't compared, so a test says only what it means to.

`iki plugin validate` doesn't read `tests/`. `iki plugin test` checks
each fixture against the plugin before running any: the action, the
param and input names, the fields of each value, and that each file
exists.

**The template's CI runs them.** First-party plugins ship fixtures, and
Iki's CI runs them with the worker it builds.

## Consequences

- Positive: a test is a few lines of what matters, and it can't drift
  from the manifest, since the run input is made from it.
- Positive: it runs the command exactly as a worker does, so a pass means
  something on a deployment's workers too.
- Negative: the command runs on the author's machine, with whatever its
  environment needs installed there. Running each test in its declared
  environment is left to the worker's tiers.
- Negative: comparing a subset can miss a value nobody wrote down. That's
  deliberate: a test that spells out every value breaks on every change.
