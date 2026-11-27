# Protein production

A protein production core expresses and purifies proteins for the projects it serves.
Each project sends the protein it needs and how much. The core returns the lot it made,
with its lot number and the mass delivered, and a reviewer at the core signs it off. Use
this plugin when your core keeps those as fields rather than only in its delivery report.

Iki's [Requests](../../../plugins/requests/README.md) types hold what every service
shares. This plugin adds one core's own fields and checks to them
([ADR 0235](../../../docs/adr/0235-a-services-own-request-types-carry-its-fields.md)).

## What it adds

- **Production request** (`ProductionRequest`, `PPR-`) extends Research request. It
  sends exactly one protein, a registered `iki/protein.Protein`, in its records, and a
  positive requested mass in mg. The asking project fills both in.
- **Production deliverable** (`ProductionDeliverable`, `PPD-`) extends Research
  deliverable. It names the protein produced, which must be the one the request sent.
  A fulfilled production also gives the lot number and a positive delivered mass. An
  unsuccessful one gives neither, and its report says why. It answers production
  requests only, and a production request takes no plain Research deliverable.

The deliverable's checks run as it's saved and again as the reviewer signs it off, so a
request changed in the meantime can't be signed off with the old answer.

## Set up the core

1. Install the plugin with `iki/requests` and `iki/protein`, and their dependencies
   `iki/inventory`, `iki/lab_roles` and `iki/dna`.
2. In the core's project, under **Settings → Request intake**, accept Production
   requests from the projects it serves.
3. Give the core's people their lab roles in its project: Coordinators accept and
   decline requests, Analysts do the work, and Reviewers sign off deliverables.

## Adapt it for your core

- To record more, such as the expression host, the purification method or the
  concentration, add fields to `ProductionDeliverable`, and a rule for each one a
  fulfilled lot must have. Give a rule `recheck_before_signing: true` when it reads the
  request, so the sign-off checks the request as it is then.
- For production under QC, with an analyst's submission, an independent review and QA's
  release, extend `iki/requests.QCRequest` and `iki/requests.QCDeliverable` instead.
- Choose HRID prefixes unique to your deployment.

## Test and deploy it

From the root of Iki's checkout:

```sh
uv run --locked python scripts/ci/check_plugin_examples.py examples/plugins/protein_production
```

That validates the plugin with its dependencies, checks its migration and runs
`tests/test_production_rules.py`. The tests save requests and deliverables against the
rules, so keep them in your copy and change the expected outcomes when your rules
change. To copy, rename, migrate and deploy the plugin, follow the
[examples guide](../README.md#copy-and-name-a-plugin). Discard the example's
`migrations/` after renaming it, and make the initial migration under your plugin's
name.

The [core facility files](../../core-facility-requests/README.md) are a worked
production: the UniProtKB P00330 sequence and a training delivery report.
