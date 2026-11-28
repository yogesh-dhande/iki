# Sequencing service

A sequencing core reads the tubes its projects send, by Sanger, short-read or long-read
sequencing. It returns each tube's reads, as uploaded files or the accession of a public
read archive run. An analyst signs each delivery, an independent reviewer reviews it and
someone in QA releases it. Use this plugin when your core keeps the method and the reads
as fields rather than only in its delivery report.

Iki's [Requests](../../../plugins/requests/README.md) types hold what every service
shares. This plugin adds one core's own fields and checks to them
([ADR 0235](../../../docs/adr/0235-a-services-own-request-types-carry-its-fields.md)).

## What it adds

- **Sequencing request** (`SequencingRequest`, `SQR-`) extends Request. It names the
  sequencing method and the tubes the core will receive.
- **Sequencing deliverable** (`SequencingDeliverable`, `SQD-`) extends Deliverable,
  one for each tube. It names the tube sequenced, or its recorded aliquot. A fulfilled
  tube has up to 12 read files or a public INSDC run accession (`SRR`, `ERR` or `DRR`),
  which links to the European Nucleotide Archive. It answers sequencing requests only,
  and a sequencing request takes no other deliverable.

The check for reads runs as a deliverable is saved and again at each signature.

## Set up the core

1. Install the plugin with `iki/requests`, and its dependencies `iki/inventory` and
   `iki/lab_roles`.
2. In the core's project, under **Settings → Request intake**, accept Sequencing
   requests from the projects it serves.
3. Under **Settings → Starters**, apply **Service deliverables** and choose how the core
   reviews what it returns, such as Regulated QC. Each request is pinned to that choice
   as it arrives, and its deliverables follow it.
4. Give the core's people their lab roles in its project: Coordinators accept requests
   and receive the tubes, Analysts submit deliverables, Reviewers review them and QA
   releases them.

## Adapt it for your core

- A research core that signs a delivery off once chooses Research in the starter. The
  same types serve both.
- Change `method` to the platforms your core runs, or add a read length, a coverage
  target or a library kit to the request.
- To check the reads themselves, add an action that reads the files, as
  [`iki/sequencing`](../../../plugins/sequencing/README.md) does for its runs.
- Choose HRID prefixes unique to your deployment.

## Test and deploy it

From the root of Iki's checkout:

```sh
uv run --locked python scripts/ci/check_plugin_examples.py examples/plugins/sequencing_service
```

That validates the plugin with its dependencies, checks its migration and runs
`tests/test_delivery_rules.py`. Keep the tests in your copy and change the expected
outcomes when your rules change. To copy, rename, migrate and deploy the plugin, follow
the [examples guide](../README.md#copy-and-name-a-plugin). Discard the example's
`migrations/` after renaming it, and make the initial migration under your plugin's
name.

The [core facility files](../../core-facility-requests/README.md) are a worked
delivery: the report for public long-read run ERR13376132.
