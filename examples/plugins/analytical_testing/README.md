# Analytical testing

An analytical core measures the material its projects send against a panel of methods:
SEC purity, intact mass, peptide map coverage and endotoxin, here. The project asks for
the analyses it needs. The core returns one result for each measurement, with its value
and whether it passed. An analyst signs it, an independent reviewer reviews it and
someone in QA releases it. Use this plugin as the starting point for your own panel.

Iki's [Requests](../../../plugins/requests/README.md) types hold what every service
shares. This plugin adds one core's own fields and checks to them
([ADR 0235](../../../docs/adr/0235-a-services-own-request-types-carry-its-fields.md)).
A result here is the service's measurement for the project that asked. A lot's
disposition stays with [`iki/lots`](../../../plugins/lots/README.md) and its QA.

## What it adds

- **Analytical request** (`AnalyticalRequest`, `ANR-`) extends QC request. It names at
  least one analysis to run on the materials or tubes it sends.
- **Analytical result** (`AnalyticalResult`, `ANT-`) extends QC deliverable. It names
  the sample measured, one analysis the request asks for, that analysis's own
  measurement and whether it passed. Another analysis's value can't stand in for it. It
  answers analytical requests only, and an analytical request takes no plain QC
  deliverable.

The result's checks run as it's saved and again at each signature, so a request whose
analyses changed after the measurement can't be signed with the old answer.

## Set up the core

1. Install the plugin with `iki/requests`, and its dependencies `iki/inventory` and
   `iki/lab_roles`.
2. In the core's project, under **Settings → Request intake**, accept Analytical
   requests from the projects it serves.
3. Give the core's people their lab roles in its project: Coordinators accept requests,
   Analysts submit results, Reviewers review them and QA releases them.

## Adapt it for your core

- Change `analyses` on the request and `analysis` on the result to your core's methods.
  Give each method its measurement field and a rule that a result for it has one.
- A method with acceptance limits can say so in a rule that holds a result outside them
  for QA's review (`hold`), rather than refusing it.
- Keep `recheck_before_signing: true` on every rule that reads the request.
- Choose HRID prefixes unique to your deployment.

## Test and deploy it

From the root of Iki's checkout:

```sh
uv run --locked python scripts/ci/check_plugin_examples.py examples/plugins/analytical_testing
```

That validates the plugin with its dependencies, checks its migration and runs
`tests/test_result_rules.py`. The tests save results for each analysis, with and without
its own measurement, and for analyses the request didn't ask for. Keep them in your
copy and change the expected outcomes when your panel changes. To copy, rename, migrate
and deploy the plugin, follow the [examples guide](../README.md#copy-and-name-a-plugin).
Discard the example's `migrations/` after renaming it, and make the initial migration
under your plugin's name.
