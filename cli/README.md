# iki-cli

The `iki` command-line tool. For now it has one command for plugin authors,
`iki plugin validate`; `build`, `makemigrations`, `test`, `upgrade` and `run`
follow as the plugin system grows.

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

```console
$ iki --version
iki 0.1.0.dev0
```
