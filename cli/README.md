# iki-cli

The `iki` command-line tool, for plugin authors and deployments: `iki plugin validate`,
`makemigrations`, `test`, `build` and `preflight`; `iki deploy build`; `iki bundle verify`; and
`iki starter` for setting up an assay.

```sh
uv tool install iki-cli==0.1.0
iki plugin validate . --with path/to/iki/plugins/dna
```

Every command and option, with what each prints and its exit codes, is in
[the iki CLI reference](https://ikisci.org/reference/cli/). How to use them on a plugin is in
[Write a plugin](https://ikisci.org/guides/write-a-plugin/). The reference's source is
[`docs/site/src/content/reference/cli.mdx`](../docs/site/src/content/reference/cli.mdx): change it
in the same commit as a command or option.
