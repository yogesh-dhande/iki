# iki-cli

The `iki` command-line tool for plugin authors and deployments: the
`iki plugin` commands `validate`, `build`, `makemigrations`, `preflight` and `test`,
`iki bundle verify`, and `iki deploy build`. `upgrade` and `run` follow as the plugin system grows.

## `iki plugin validate`

Checks every manifest in a plugin against the `iki/v1` spec (see
[`spec/`](../spec/README.md)): the JSON Schemas, UCUM units, references
between types and plugins, the rules for tables, indexes, identities and
settings, and what its actions read, write and run in.

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
Migrations for 'iki__protein':
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

## `iki plugin preflight`

Check a built plugin's manifest against the records in a deployment before
promoting it there. Run as an Org Admin with access to all projects.

```sh
iki plugin preflight build/plugins/iki_plugin_iki__dna/manifest.json --url https://iki.example.org --json
```

Sign in through the Python SDK with
`iki.login("https://iki.example.org", scopes=["admin"])` or set `IKI_URL`
and `IKI_TOKEN` with an all-project token with the `admin` scope. The CLI
groups findings by type, with an affected count and up to five example
HRIDs for each. SQL checks give exact counts. For checks that require
a sample, the report names the check and gives the number of records
examined. A partial sample gives a lower bound for that finding. The
CLI exits with 0 for a clear report, 1 when the deploy requires
acknowledgement, and 2 for input, connection or access errors. The API
accepts the same manifest as the JSON body of
`POST /api/v1/plugins/{namespace}/{name}/preflight`.
See the [promotion procedure](../deploy/config-template/README.md#how-a-change-reaches-a-deployment).

## `iki plugin build`

Compiles plugins into Django apps for the server: a package
`iki_plugin_<namespace>__<name>` per plugin (its label, as `iki/protein` is
`iki__protein`), with its models and migrations, and
`iki_installed_plugins`, which the server installs. Give every plugin a
deployment installs; their dependencies must be among them.

```console
$ iki plugin build plugins/dna plugins/protein --output build/plugins
Built build/plugins/iki_plugin_iki__dna
Built build/plugins/iki_plugin_iki__protein
```

## `iki plugin test`

Runs the plugin's action tests: each `kind: ActionTest` fixture in its
`tests/` directory ([ADR 0093](../docs/adr/0093-plugin-action-tests-run-fixtures-through-the-worker.md),
and the [spec](../spec/README.md#action-tests)). For each, it makes the
run input the server would, runs the action with `iki-worker run --local`,
and checks what it writes: against `run-output/v1`, against what the action
declares it changes, and against the test's `expect`.

```console
$ iki plugin test plugins/protein --with plugins/dna
Running 2 test(s) of iki/protein 0.8.0:
  ok      haemoglobin_alpha  (0.3s)
  FAILED  cystines  (0.2s)
          tests/compute_properties.yaml
          updates.protein[0].values.extinction_coefficient_oxidized: expected 7115, got 6990
1 passed, 1 failed in 0.5s.
```

The commands run on this machine, so install what their environments need
first. It exits 1 if any test fails, or if a fixture doesn't match the
plugin, in which case nothing runs.

| Option               | What it's for                                                        |
| -------------------- | -------------------------------------------------------------------- |
| `--with DIR`         | A plugin this one depends on, directly or not. Repeat for each.      |
| `-k TEXT`            | Run only the tests whose name contains this.                         |
| `--worker PATH`      | The `iki-worker` binary. `$IKI_WORKER`, or `iki-worker` on the PATH. |
| `--upgrade-from REF` | Also test the upgrade from the plugin at this git commit (below).    |
| `--database URL`     | The PostgreSQL server for it. `$IKI_TEST_DATABASE_URL` unless given. |
| `--no-rollback`      | Don't roll the upgrade back: for one meant to be one-way.            |

With `--upgrade-from`, it also tests the plugin's upgrade from an earlier
commit, such as a pull request's base
([ADR 0094](../docs/adr/0094-a-plugins-upgrade-is-tested-from-an-earlier-commit.md)).
On a scratch database it makes on the `--database` server, it deploys the
plugin as it was then and creates that commit's action-test entities as
samples. It then deploys the plugin as it is now, and checks every sample
is still there with its history. Last, it deploys the earlier commit again,
as a rollback, and checks the samples' values came back. The database is
dropped at the end.

Before it migrates anything, it compares each type then and now
([ADR 0095](../docs/adr/0095-a-plugin-says-which-iki-it-works-with-and-versions-its-changes.md)).
If what a type's entities may hold changed, such as a field added or a
rule tightened, its `schema_version` must be higher: plugins that depend on
it pin schema versions. A new label, description, display unit, link or
index doesn't count. If the migrations changed and the plugin's `version`
didn't, it says so, and passes.

```console
$ iki plugin test plugins/protein --with plugins/dna --upgrade-from origin/main \
    --database postgres://postgres@localhost:5432/postgres
…
Upgrading protein from 3f2a1c9b7e0d (0.7.0) to 0.8.0 on the scratch database
iki_upgrade_5c1e9a07, with 2 sample(s):
  ok      each type whose fields changed has a higher schema_version
  ok      migrated to the earlier commit and made the samples  (9.9s)
  ok      upgraded: the samples and their history are intact  (1.1s)
  ok      rolled back: the samples have their values  (0.8s)
```

## `iki bundle verify`

Checks a bundle exported from Iki
([ADR 0096](../docs/adr/0096-bundles-carry-entities-between-deployments-with-their-history.md))
and the electronic signatures it carries
([ADR 0126](../docs/adr/0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md)),
without Iki: for an auditor, a partner, or an archive years later. It checks
the bundle's own signature and every member against its manifest, then each
signature: its key signed it, it names its record, revision and change, the
content it signed hashes to what it names, and the bundle's history of the
record is still that content, every value of it, read through what was done
to it since (plugins' migrations, imports naming people and registry entries
as they are where they landed). A key you trust must vouch for that too: the
signing key of the deployment that exported the bundle
([ADR 0148](../docs/adr/0148-a-signature-reads-its-record-through-what-migrations-did-since.md)).

```console
$ iki bundle verify lot-a.tar.gz --trust prod=ed25519:riAtz4…
The bundle checks out, signed by prod's key.
ok   LOT-000001: Authored by Ada Lovelace on 2026-11-18 at 14:03:22 UTC. Meaning: Authorship. (carried)
ok   LOT-000001: Approved by Grace Hopper on 2026-11-18 at 15:10:04 UTC. Meaning: QA approval. (carried)
2 signatures, all valid.
```

| Option                   | What it does                                                                                                             |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `BUNDLE`                 | The bundle: a `.tar.gz` exported from Iki.                                                                               |
| `--trust DEPLOYMENT=KEY` | Trust KEY as DEPLOYMENT's bundle key; the signing keys the bundle lists are then trusted too.                            |
| `--key KEY`              | Trust KEY for signatures, and for what was done since, whatever the bundle lists: from `GET /api/v1/signing-keys` there. |
| `--json`                 | Print the result as JSON, with every check.                                                                              |

It exits with 0 when the bundle and every signature check out, 1 when
anything doesn't (an untrusted key included), and 2 for a usage mistake.

## `iki deploy build`

In a deployment's config repository, build the plugins its
`plugins.lock.yaml` pins (see [`spec/`](../spec/README.md#a-deployments-lockfile)),
for its server image:

```console
$ iki deploy build deployments/prod --output build/plugins
Fetched iki/dna at 4f1c2b9e0d6a from https://github.com/yogesh-dhande/iki.git
Fetched acme/assays at 9b2e4c1d0a7f from https://github.com/acme/iki-assays.git
Built build/plugins: iki/dna 0.4.2, acme/assays 1.2.0.
$ docker build -f plugins.Dockerfile \
    --build-arg IKI_SERVER_IMAGE=ghcr.io/yogesh-dhande/iki-server:0.1.0@sha256:… build
```

It fetches each plugin at its commit and checks them together, along with
the deployment's settings for each and that their migrations are complete.
If the deployment has a `compute.yaml`, it checks its templates and bindings
against the plugins' environments and actions
([spec](../spec/README.md#a-deployments-compute)), and writes them as
`compute.json` for the server. It writes the same bytes for the same lockfile. Git may only use https and
ssh, and never prompts; credentials come from git's helpers, as CI sets
them up. See [ADR 0034](../docs/adr/0034-deployment-lockfile-and-plugin-builds.md).
Use the server image of the same Iki version: the server refuses plugins
another version built.

| Option                         | What it does                                                                   |
| ------------------------------ | ------------------------------------------------------------------------------ |
| `DEPLOYMENT`                   | The deployment's directory, holding `plugins.lock.yaml` (or the lockfile).     |
| `--output DIRECTORY`           | Where to write the plugins. Replaces an earlier build there, and nothing else. |
| `--workers DIRECTORY`          | Also write what the deployment's workers need (below).                         |
| `--images FILE`                | Also list the images its container-tier runs use, for its build to approve.    |
| `--environments-dir DIRECTORY` | Where the worker image builds environments (default `/opt/iki/envs`).          |
| `--allow-local`                | Accept `local:` plugins, for development. Servers refuse them unless told to.  |
| `--mirror URL`                 | Fetch Iki's own plugins from this mirror of Iki's repository. Repeatable.      |
| `--cache DIRECTORY`            | Where to keep fetched repositories (default `~/.cache/iki/git`).               |

With `--mirror`, Iki's own plugins (`iki/...`) come from the mirrors given,
in order, and never from Iki's repository: the deployment's
`plugins.mirrors`, for a site that can't reach github.com. Each must be at
the commit the lockfile pins, with a tree Iki's repository has had, as this
`iki-cli` knows them, so a mirror can't serve a commit of its own. The build
records where each plugin came from, and the server refuses plugins from a
mirror its deployment doesn't name
([ADR 0114](../docs/adr/0114-ikis-own-plugins-can-come-from-a-mirror.md)).

With `--workers build/worker`, it also writes what the deployment's
backend workers need, from the same commits as the server, so the two
agree on what each run's code is
([ADR 0047](../docs/adr/0047-workers-run-only-the-code-a-run-was-launched-with.md)):

- `plugins/<label>/`: each plugin's code at its commit, by its label
  (`iki__protein`);
- `plugins/plugins.json`: what the worker tells the server it has;
- `build-environments.sh`: builds each Python or conda environment the
  plugins declare for the backend or self-hosted tier, installing only the
  packages its requirements file or lockfile pins, by hash.

The worker image's Dockerfile copies `plugins/` to `/opt/iki/plugins` and
runs the script.

```console
$ iki --version
iki 0.1.0.dev0
```
