# Iki

Iki is an open-source platform for scientific data. Scientists work with typed
**entities** (DNA, proteins, antibodies, compounds, datasets) and run **actions**
on them. Every action run, every entity revision and the origin of every property
is recorded, so the full lineage of any result can be traced.

The core defines only base types and logic. Enterprises extend it with
**plugins**: git repos of YAML manifests that add entity types, fields, actions,
compute environments and UI components. Actions are language-agnostic and run on
the user's machine, in isolated backend workers, in containers on Kubernetes, or
on cloud VMs.

People use Iki through a web UI and a Python SDK. AI agents use it through an MCP
server.

> **Status:** early development. Nothing here is ready for production use yet.

## Repository layout

| Path               | What it holds                                                        |
| ------------------ | -------------------------------------------------------------------- |
| `server/`          | Django project (`iki_server`): REST API, worker API, MCP server      |
| `cli/`             | The `iki` command-line tool (`iki_cli`)                              |
| `spec/`            | The versioned plugin specs (JSON Schemas), their validator, UCUM     |
| `codegen/`         | Compiles plugin manifests into Django apps (`iki_codegen`)           |
| `web/`             | React host app and the `@ikisci/*` TypeScript packages               |
| `worker/`          | Rust workspace: the `iki-worker` agent and `iki-protocol` wire types |
| `deploy/`          | Helm chart, dev cluster, and a deployment's config repo template     |
| `plugins/`         | Iki's first-party plugins, such as `dna`                             |
| `plugin-template/` | Where a lab's own plugin starts: example types, migrations, CI       |
| `docs/adr/`        | Architecture Decision Records                                        |

More directories (`sdk/python/`) arrive
as the features that need them land.

## Development

Requirements:

- [uv](https://docs.astral.sh/uv/) 0.13.7 or later (it installs Python 3.14 for you)
- Node.js 24 LTS with [Corepack](https://nodejs.org/api/corepack.html) enabled
  (`corepack enable`), which provides the pinned pnpm
- [rustup](https://rustup.rs/), which installs the Rust toolchain pinned in
  `worker/rust-toolchain.toml`
- Helm, plus the supply-chain scanners used by `make check`:
  `brew install helm osv-scanner cargo-deny grype syft`

```console
make setup   # install dependencies from the lockfiles
make check   # run every check CI runs: lint, format, types, tests
make fmt     # format all code
make help    # list all tasks
```

To run the whole stack on a local Kubernetes cluster (Postgres, S3, Keycloak,
Grafana), see [deploy/dev/README.md](deploy/dev/README.md): `make dev-up`.

## Contributing

We aren't accepting pull requests from outside contributors yet, but issues
are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). By taking part you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md). To report a vulnerability, follow
[SECURITY.md](SECURITY.md). Do not open a public issue for it.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
