# 0020. Python package names: `iki` belongs to the SDK

- Status: Accepted
- Date: 2026-11-09

## Context

Iki has three Python distributions: the client SDK, the Django server, and
the `iki` CLI. Users install the SDK in notebooks and scripts. Plugin
authors often install the CLI and the SDK, and sometimes the server, in the
same environment. Python has one flat namespace for top-level imports. If
two installed packages provide the same top-level name, one silently
replaces the other and imports break in confusing ways.

The SDK is the package most users touch, and `import iki` is the name they
should type.

## Decision

- **Client SDK**: published on PyPI as `ikisci`, imported as `import iki`.
  It owns the `iki` import name.
- **Django project package**: `iki_server`, in `server/src/iki_server`.
- **CLI package**: `iki_cli`, which installs the console script `iki`.
- No other Iki package may provide a top-level `iki` module or package.

## Consequences

- Positive: the SDK, CLI and server can be installed together without
  shadowing each other.
- Positive: users get the short, obvious `import iki`.
- Positive: the command people type (`iki`) and the import they write
  (`import iki`) match the product name.
- Negative: the PyPI name (`ikisci`) differs from the import name (`iki`),
  which needs a clear note in the docs.
- Negative: server and CLI code uses the longer `iki_server` and `iki_cli`
  names.

## Alternatives considered

- **Naming the Django project package `iki`.** Rejected. It would clash with
  the SDK's import name whenever both are installed.
