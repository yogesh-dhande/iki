# iki-cli

The `iki` command-line tool for plugin authors and deployments: `iki plugin
validate`, `build` and `makemigrations`, and `iki deploy build`. `test`,
`upgrade` and `run` follow as the plugin system grows.

## `iki plugin validate`

Checks every manifest in a plugin against the `iki/v1` spec (see
[`spec/`](../spec/README.md)): the JSON Schemas, UCUM units, references
between types and plugins, and the rules for tables, indexes, identities and
settings.

```console
$ iki plugin validate plugins/protein --with plugins/dna
plugins/protein/types/protein.yaml:12:45: error: `Da` isn't a UCUM unit. [unknown-unit]
 12 |   molecular_weight: { type: quantity, unit: Da, computed: true }
    |                                             ^
  hint: `u` is the unified atomic mass unit (the dalton); `g/mol` is molar mass.

1 error in plugins/protein.
```

| Option             | What it does                                                                  |
| ------------------ | ----------------------------------------------------------------------------- |
| `DIRECTORY`        | The plugin's directory, holding `plugin.yaml`. Defaults to the current one.   |
| `--with DIRECTORY` | A plugin this one depends on, so references to it can be checked. Repeatable. |
| `--settings FILE`  | Also check a deployment's values for the plugin's settings (a YAML mapping).  |
| `--format text`    | For people: file, line and column, the source line, and a hint (the default). |
| `--format json`    | For tools: every problem with its stable `code`, location and path.           |
| `--format github`  | GitHub Actions annotations, so problems show on the pull request's diff.      |
| `--strict`         | Fail on warnings too.                                                         |

It exits with 0 when the plugin is valid, 1 when there are problems (or
warnings, with `--strict`), and 2 for a usage mistake. Colors follow
`NO_COLOR` and `FORCE_COLOR`.

## `iki plugin makemigrations`

After changing a plugin's manifests, make the migration that changes the
database to match, and commit it with the change. It needs no database.

```console
$ iki plugin makemigrations plugins/protein --with plugins/dna
Was protein.organism renamed to protein.host_organism (a CharField)? [y/N] y
Migrations for 'protein':
  plugins/protein/migrations/0002_rename_organism_protein_host_organism_and_more.py
    - Remove constraint protein_protein__organism__not_empty from model protein
    - Remove constraint protein_protein__organism__one_line from model protein
    ~ Rename field organism on protein to host_organism
    ~ Rename field organism on protein_history to host_organism
    + Create constraint protein_protein__host_organism__not_empty on model protein
    + Create constraint protein_protein__host_organism__one_line on model protein
```

It runs Django's makemigrations, with its questions about renames, and adds
Iki's rules for history (see [ADR 0033](../docs/adr/0033-plugin-codegen-and-migrations.md)):

- To remove a field, mark it `removed: true`. Its column goes and its
  history stays. A field that simply disappears is refused.
- Types and extensions can't be deleted.
- A field's type can only change when its history can follow: text stays
  text, and whole numbers may become floats.
- A quantity's unit can change, and the migration converts the stored
  values, current and historical.
- Renaming a type renames its entities' type too.

| Option             | What it does                                                                    |
| ------------------ | ------------------------------------------------------------------------------- |
| `DIRECTORY`        | The plugin's directory. Defaults to the current one.                            |
| `--with DIRECTORY` | A plugin this one depends on, directly or not, with its migrations. Repeatable. |
| `--name NAME`      | A name for the migration.                                                       |
| `--check`          | Write nothing; fail if the manifests have changes no migration has (for CI).    |
| `--dry-run`        | Show what the migration would do; write nothing.                                |
| `--no-input`       | Ask nothing: renames aren't detected, and questions Django needs answered fail. |
| `--merge`          | Merge migrations that branched, such as after two pull requests.                |
| `--empty`          | Write an empty migration, to fill in by hand (for data changes).                |

## `iki plugin build`

Compiles plugins into Django apps for the server: a package
`iki_plugin_<name>` per plugin, with its models and migrations, and
`iki_installed_plugins`, which the server installs. Give every plugin a
deployment installs; their dependencies must be among them.

```console
$ iki plugin build plugins/dna plugins/protein --output build/plugins
Built build/plugins/iki_plugin_dna
Built build/plugins/iki_plugin_protein
```

## `iki deploy build`

In a deployment's config repository, build the plugins its
`plugins.lock.yaml` pins (see [`spec/`](../spec/README.md#a-deployments-lockfile)),
for its server image:

```console
$ iki deploy build deployments/prod --output build/plugins
Fetched dna at 4f1c2b9e0d6a from https://github.com/acme/iki-plugins.git
Fetched protein at 4f1c2b9e0d6a from https://github.com/acme/iki-plugins.git
Built build/plugins: dna 1.2.0, protein 3.0.0.
$ docker build -f plugins.Dockerfile \
    --build-arg IKI_SERVER_IMAGE=ghcr.io/ikisci/iki-server:0.1.0@sha256:… build
```

It fetches each plugin at its commit and checks them together, along with
the deployment's settings for each and that their migrations are complete.
It writes the same bytes for the same lockfile. Git may only use https and
ssh, and never prompts; credentials come from git's helpers, as CI sets
them up. See [ADR 0034](../docs/adr/0034-deployment-lockfile-and-plugin-builds.md).
Use the server image of the same Iki version: the server refuses plugins
another version built.

| Option               | What it does                                                                   |
| -------------------- | ------------------------------------------------------------------------------ |
| `DEPLOYMENT`         | The deployment's directory, holding `plugins.lock.yaml` (or the lockfile).     |
| `--output DIRECTORY` | Where to write the plugins. Replaces an earlier build there, and nothing else. |
| `--allow-local`      | Accept `local:` plugins, for development. Servers refuse them unless told to.  |
| `--cache DIRECTORY`  | Where to keep fetched repositories (default `~/.cache/iki/git`).               |

```console
$ iki --version
iki 0.1.0.dev0
```
