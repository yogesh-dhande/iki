# iki-codegen

Compiles Iki plugin manifests (see [`spec/`](../spec/README.md)) into Django
apps. `iki plugin build` and `iki plugin makemigrations` use it; the design
is in [ADR 0033](../docs/adr/0033-plugin-codegen-and-migrations.md).

For a plugin `protein`, it writes the package `iki_plugin_protein` (app
label `protein`), with a model per table, and `iki_installed_plugins`,
which lists the apps the server installs.
