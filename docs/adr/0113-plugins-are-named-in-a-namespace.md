# 0113. Plugins are named in a namespace, and `iki` is Iki's own

- Status: Accepted
- Date: 2026-11-17
- Refines: [0033](0033-plugin-codegen-and-migrations.md),
  [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0038](0038-core-plugin-app-and-collections.md),
  [0054](0054-run-secrets.md),
  [0059](0059-workers-fetch-plugin-code-and-build-environments.md),
  [0094](0094-a-plugins-upgrade-is-tested-from-an-earlier-commit.md)

## Context

A plugin had one flat name, such as `protein`. It named the plugin's types
(`protein.Protein`), its Django app, its tables (`protein_protein`) and its
package. Iki's own plugins took the obvious names: `dna`, `protein`,
`antibody`. A lab writing its own plugin for proteins, or two vendors each
shipping an `assays` plugin, could not install it beside the other. Nothing
in a name said whose a plugin was, so a lab's `dna` could also pass for
Iki's.

Other plugin systems avoid this the same way:

- Terraform names providers `namespace/name` (`hashicorp/aws`).
- GitHub Actions names actions `owner/repo`.
- Kubernetes makes third-party API groups use a domain the author owns.

Iki had no deployments yet, so the names could change everywhere at once,
with nothing to migrate.

## Decision

**A plugin is named `namespace/name`.** Both parts are lowercase
identifiers, as names were before: 2 to 30 letters, digits and single
underscores, starting with a letter. Iki's own plugins are `iki/core`,
`iki/dna`, `iki/protein`, `iki/antibody`, `iki/small_molecule` and
`iki/inventory`. The plugin template is `acme/example`; its README says
to use your lab's or company's namespace in place of the placeholder.
Everything else follows from the name:

- types: `iki/protein.Protein`
- actions: `iki/protein.translate`
- environments: `iki/protein.structure-tools`
- workflows, triggers and components, the same way

Splitting on the first `.` still finds the plugin, because neither part of
a name has a dot.

**References name another plugin's things in full.** Terraform lets a
module give a provider a local name. Iki doesn't: a manifest is read on its
own, in reviews and diffs, so it should say exactly what it means.

- A plugin's own types, actions and the rest are written short (`Protein`)
  or in full (`acme/proteins.Protein`).
- Anything of another plugin's is written in full (`iki/dna.DNASequence`),
  dependencies included (`dependencies: { iki/dna: … }`).
- A run's output names the types it creates in full.
- The validator refuses anything else, and says what to write:
  - a short reference (`dna.DNASequence`) is `unqualified-reference`;
  - a short dependency is `unqualified-dependency`;
  - a plugin named without a namespace is `unqualified-name`.
- The hint gives the full name when exactly one known plugin has that short
  name (a dependency given with `--with`, else one of Iki's). When two do
  (`acme/dna` and `globex/dna`), it asks for `<namespace>/dna`.

**The `iki` namespace is reserved, and enforced in two places.**

- **The validator.** It refuses `iki/…` for any plugin but the six above,
  and a namespace that starts with `iki_`, which could pass for Iki's
  (`reserved-namespace`). Even those six names are refused outside Iki's own
  build of them. Only the core plugin's own build may call itself
  `iki/core`.
- **The lockfile.** It takes an `iki/…` plugin only from Iki's repository,
  `https://github.com/yogesh-dhande/iki.git`, at `plugins/<name>`, however
  the URL is written (https, ssh or scp form, with or without `.git`). A
  fork is another plugin, so it needs a namespace of its own
  (`first-party-source`). A `local:` directory, for development, and a
  purged plugin, which isn't fetched, are taken.

A site that can't reach Iki's repository fetches Iki's plugins from mirrors
of it that its deployment's admin names, and the lockfile still names Iki's
repository ([ADR 0114](0114-ikis-own-plugins-can-come-from-a-mirror.md)).

Short names reserved for Django's and Iki's apps (`auth`, `core`, `admin`,
keywords) are no longer reserved. A plugin's app label now always has a
`__` in it, which no app of Django's or Iki's does, so `acme/auth` can't
clash with anything.

**Storage uses one rule for every namespace: the label `namespace__name`.**
A name with a `/` can't be a Django app label, a Python package, a table
name, a directory or a Kubernetes Secret key. So everywhere it has to be one
of those, a plugin goes by its label, `iki/protein` becoming `iki__protein`:

| What                       | For `iki/protein` (and `acme/lims` extending it)     |
| -------------------------- | ---------------------------------------------------- |
| Django app label           | `iki__protein`                                       |
| Package                    | `iki_plugin_iki__protein`                            |
| Type table                 | `iki__protein_protein`                               |
| History, lists             | `iki__protein_protein__history`, `…__chains`         |
| Extension table            | `acme__lims__iki__protein_protein`                   |
| Worker bundle and code dir | `iki__protein-<sha>.tar.gz`, `plugins/iki__protein/` |
| Environment prefix         | `/opt/iki/envs/iki__protein/structure-tools`         |
| Run secret key and file    | `iki__protein.UNIPROT_TOKEN`                         |

Django's field names can't hold `__`, so the few names Django makes from a
plugin use `namespace_name` instead:

- an extension's model, `Ext_iki_protein_Protein`;
- its related name, `acme_lims_Ext`;
- a parent link that would clash, `acme_dna_sequence_ptr`.

Two plugins whose `namespace_name` would coincide (`acme_bio/assays` and
`acme/bio_assays`) can't be installed together, and the build says so.

The alternative was to keep Iki's own tables unprefixed (`protein_protein`)
and prefix only other namespaces' tables. We didn't take it:

- **It's two rules.** Code and people would have to know which one applies.
- **Tables wouldn't say whose they are.** With one rule, a DBA or auditor
  reading `iki__protein_protein` or `acme__assays_run` knows which plugin
  owns it.
- **It wouldn't help where help is needed.** The names that run out of
  room are the long ones: third-party plugins extending other plugins'
  types. They'd be prefixed either way.
- **It keeps clashes out of reach.** `iki__core_view` can't collide with
  the server's own `iki_*` tables. A lab's `acme/core` is `acme__core_view`.

The cost is length. The longest label is 62 characters, and PostgreSQL
allows 63 for any identifier. Each name begins with the label:

- **Iki's own.** Its longest first-party table is
  `iki__antibody__iki__protein_proteinstructure__history`, at 53.
- **A lab's.** An extension's history, such as
  `acme__lims__iki__protein_protein__history`, is 41. That leaves room for a
  type name of around 20 characters.
- **Too long.** The validator refuses a name over 63 characters and says
  which part to shorten (`name-too-long`), so nothing is ever truncated.
- **Constraints.** A CHECK constraint whose name would be too long keeps its
  start and gains a short hash, as before.

**The API takes names as they are.** A name in a path has its `/` raw:

- `/api/v1/entity-types/iki/protein.Protein`
- `/api/v1/actions/iki/protein.translate/runs`
- `/worker/v1/plugins/iki/protein/<sha>`
- the web app's `/types/iki/protein.Protein/`

The server's `plugin` and `qualified` path converters match one name.

- **Escaped slashes.** The server decodes `%2F` before matching, so an
  escaped `/` reaches the same view.
- **Iki's clients.** Our SDKs and the web app send the `/` raw.
- **The MCP bridge.** Its internal requests now pass a decoded path, as a
  real server does. Before, an escaped name wouldn't match.
- **MCP tools.** Their names don't change. Their arguments carry the full
  names.

An unqualified name (`protein.Protein`) still matches the route. The view
then says it doesn't know it, rather than giving a bare 404.

**Identities and run reuse use full names.** An entity's identity hash, and
the key a run is reused by, include its type's or action's full name.
Nothing was hashed under the old names.

**No legacy names.** There's no mapping from `protein` to `iki/protein`. No
migration rewrites old names, no reader takes old manifests, and no bundle
import maps old types. Every first-party migration, the core plugin's and
the template's, was regenerated as a fresh `0001_initial`. That also
removed the core plugin's one data migration, which made views of one
entity into views of a list (ADR 0110): no database had any. A fresh
database migrates cleanly, and `makemigrations --check` finds nothing to
make.

**The upgrade test starts here.** `iki plugin test --upgrade-from REF`
(ADR 0094) has nothing to upgrade from if, at REF, the plugin's
`plugin.yaml` had no namespace, or had another name. No deployment ran it,
so the test says so and passes. Its baseline is the first commit with
namespaces.

## Consequences

- Labs name plugins in their own namespace, and two labs' plugins can share
  a short name, as `acme/assays` and `globex/assays` do.
- `iki/…` in a lockfile means Iki's code, from Iki's repository.
- Manifests are longer: `iki/dna.DNASequence` rather than `dna.DNASequence`.
  In exchange, a reader never has to work out which `dna` is meant.
- Tables, apps and packages carry `namespace__name`. A table's name says
  whose it is. A plugin with long type names extending another plugin's
  types has less room under PostgreSQL's 63 characters, and the validator
  says so when it runs out.
- Run secrets are keyed by label (`iki__dna.NCBI_API_KEY`). A deployment
  must put them under those keys.
- A worker finds plugins in `IKI_PLUGINS_DIR` by the name in each
  directory's `plugin.yaml`, so a checkout's `plugins/` works as it is.
  What `iki deploy build --workers` writes is laid out by label.
