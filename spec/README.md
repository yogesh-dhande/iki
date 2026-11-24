# iki-spec

The versioned specs for Iki plugins, and the Python package that reads and checks them.

- **JSON Schemas** (draft 2020-12), in [`src/iki_spec/schemas/`](src/iki_spec/schemas/). Each
  `<name>/v1.json` is published at `https://ikisci.org/schemas/<name>/v1`. Point an editor at
  [`manifest/v1`](src/iki_spec/schemas/manifest/v1.json) for any manifest, and at
  [`compute/v1`](src/iki_spec/schemas/compute/v1.json) for a deployment's `compute.yaml`: each picks
  the right schema by `kind`.
- **The validator** behind `iki plugin validate` (see [`cli/`](../cli/README.md)), which also checks
  what a schema can't: units, references across files and plugins, table and index rules,
  identities, settings, and what actions read, write and run in.
- **Units** ([`iki_spec.units`](src/iki_spec/units/__init__.py)): parsing, checking and converting
  [UCUM](https://ucum.org) codes. Iki reads the official UCUM table (version 2.2), which ships
  unmodified in [`src/iki_spec/units/`](src/iki_spec/units/) under the
  [UCUM License](src/iki_spec/units/UCUM-LICENSE.md). The parser and converter are Iki's own
  (Apache-2.0).

## Documentation

What the spec says is on the docs site, generated from these schemas and written from this
package's behaviour:

- [The plugin spec reference](https://ikisci.org/reference/plugin-spec/): every kind of manifest
  and document, and every key, with its type, allowed values, default and meaning. It's built from
  the schemas here, and the build fails on a key without a `description`.
- [Write a plugin](https://ikisci.org/guides/write-a-plugin/): how a plugin is laid out, named,
  validated, built and tested, and its problem codes. Then one page per topic:
  - [Declare a record type and its fields](https://ikisci.org/guides/write-a-plugin/fields/)
  - [Register records by their identity](https://ikisci.org/guides/write-a-plugin/identities-and-registries/)
  - [Track where a record is with state fields](https://ikisci.org/guides/write-a-plugin/states/)
  - [Check records with rules and guards](https://ikisci.org/guides/write-a-plugin/rules-and-guards/)
  - [Name people in records](https://ikisci.org/guides/write-a-plugin/people/)
  - [Tell people when records change](https://ikisci.org/guides/write-a-plugin/notifications/)
  - [Take requests from other projects](https://ikisci.org/guides/write-a-plugin/requests/)
  - [Keep how much of each thing there is](https://ikisci.org/guides/write-a-plugin/amounts/)
  - [Take settings from each deployment](https://ikisci.org/guides/write-a-plugin/settings/)
  - [Write an action and its environment](https://ikisci.org/guides/write-a-plugin/actions/)
  - [Read a run's input and write its results](https://ikisci.org/guides/write-a-plugin/run-input-and-output/)
  - [Test actions with fixtures](https://ikisci.org/guides/write-a-plugin/action-tests/)
  - [Chain actions in a workflow](https://ikisci.org/guides/write-a-plugin/workflows/)
  - [Run actions when records change or on a schedule](https://ikisci.org/guides/write-a-plugin/triggers/)
  - [Show records in viewers and components](https://ikisci.org/guides/write-a-plugin/viewers-and-components/)
- [Configure a deployment's plugins and compute](https://ikisci.org/guides/configure-plugins-and-compute/):
  the lockfile, compute templates, bindings, volumes and prices.
- [Bundles](https://ikisci.org/reference/plugin-spec/bundle/): what a bundle that carries records
  between deployments holds, line by line.

The pages are in [`docs/site/src/content/guides/`](../docs/site/src/content/guides/), beside the
site's other sources. When a change here changes what a plugin author or an admin does, change
the guide page in the same commit, and give every new key a `description`.
